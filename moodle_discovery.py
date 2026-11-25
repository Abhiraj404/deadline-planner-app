"""Discover enrolled courses, assignments, dates and weightings on Moodle pages."""

import re
from urllib.parse import parse_qs, urljoin, urlsplit

import dateparser
from bs4 import BeautifulSoup

from moodle_http import MoodleError
from moodle_urls import (
    ACTIVITY,
    ASSIGN,
    CA,
    COURSE,
    INTRO,
    MAIN_CONTENT,
    SECTION,
    activity_id,
    calendar_link,
)
from planner_data import SyncStopped

PERCENT = r'(\d{1,3}(?:[.,]\d+)?\s*%)'
WEIGHT_PATTERNS = [
    rf'\b(?:assessment\s+)?weight(?:ing)?\s*(?::|=|is|of)?\s*{PERCENT}',
    rf'{PERCENT}\s+(?:of|towards)\s+(?:the\s+)?(?:overall\s+|final\s+|total\s+)?(?:module|course|final|overall)\s*(?:grade|mark|assessment)?\b',
    rf'\bworth\s+{PERCENT}',
]


def clean(text):
    """Plain text from an HTML fragment."""
    return BeautifulSoup(text or '', 'html.parser').get_text(' ', strip=True)


def weighting(text):
    """A single explicit weighting percentage in the text, or None if unclear."""
    values = set()
    for pattern in WEIGHT_PATTERNS:
        for match in re.finditer(pattern, text, re.I):
            number = float(match[1].rstrip('%').strip().replace(',', '.'))
            if 0 <= number <= 100:
                values.add(f'{number:g}%')
    return next(iter(values)) if len(values) == 1 else None


def assignment_details(soup, title):
    """Read title, description, due date and weighting from an assignment page."""
    main = soup.select_one(MAIN_CONTENT) or soup
    intro = main.select_one(INTRO)
    notes = intro.get_text('\n', strip=True) if intro else ''
    heading = main.select_one('.activity-header h1, .page-header-headings h1, h2.activityname')
    title = heading.get_text(' ', strip=True) if heading else title
    due = None
    due_label_found = False
    for el in main.select('[data-region="activity-dates"] > div, .activity-dates > div, tr'):
        value = el.get_text(' ', strip=True)
        match = re.match(r'^(?:due date|due)\s*(?::\s*|\s+)(.+)$', value, re.I)
        if not match:
            continue
        due_label_found = True
        time = el.select_one('time[datetime]')
        raw = time['datetime'] if time else match[1]
        parsed = dateparser.parse(raw, languages=['en'], settings={'DATE_ORDER': 'DMY'})
        if parsed:
            due = parsed.date().isoformat()
            break
    return dict(
        title=title or 'Moodle assignment',
        notes=notes,
        due=due,
        due_unparsed=due_label_found and due is None,
        weighting=weighting(notes),
    )


def normalized_title(title):
    """Lower-case title without punctuation or a trailing "is due"."""
    return re.sub(r'\W+', ' ', re.sub(r'\s+is due\s*$', '', title, flags=re.I).casefold()).strip()


def merge_assignments(rows, assignments, modules, base):
    """Attach module names and assignment details to calendar rows."""
    module_names = {m['id']: m['name'] for m in modules}
    names = {}
    for module in modules:
        for name in [module['name'], *module.get('aliases', [])]:
            if name:
                names.setdefault(normalized_title(name), set()).add(module['id'])
    for row in rows:
        module_id = row.get('module_id') or activity_id(row.get('link'), base, COURSE)
        labels = row.pop('calendar_categories', [])
        if not module_id:
            candidates = {key for label in labels for key in names.get(normalized_title(label), [])}
            if len(candidates) == 1:
                module_id = candidates.pop()
        if module_id in module_names:
            row.update(category=module_names[module_id], module_id=module_id)
    for assignment in assignments:
        assignment_id = activity_id(assignment['link'], base, ACTIVITY)
        matches = [
            r
            for r in rows
            if assignment_id and activity_id(r.get('link'), base, ASSIGN) == assignment_id
        ]
        if matches:
            row = min(matches, key=lambda r: (r['due'] != assignment['due'], r['key']))
        else:
            candidates = [
                r
                for r in rows
                if not activity_id(r.get('link'), base, ASSIGN)
                and normalized_title(r['title']) == normalized_title(assignment['title'])
                and (
                    r['due'] == assignment['due']
                    and r.get('module_id') in (None, '', assignment['module_id'])
                    or not assignment['due']
                    and r.get('module_id') == assignment['module_id']
                )
            ]
            row = candidates[0] if len(candidates) == 1 else None
        if row is not None:
            row.update({key: value for key, value in assignment.items() if key != 'key' and value})
        elif assignment['due']:
            rows.append(assignment.copy())
    return rows


class MoodleDiscovery:
    """Find enrolled courses, assignment details and linked assessment files."""

    def links(self, soup, selector='a[href]', page_url=None):
        """Yield (absolute URL, text) for links on this Moodle site."""
        for anchor in soup.select(selector):
            try:
                url = urljoin(page_url or self.current_url, anchor['href'])
            except ValueError:
                continue
            if self.allowed(url):
                label = anchor.select_one('.instancename, .activityname')
                if label:
                    for hidden in label.select('.accesshide, .sr-only, .visually-hidden'):
                        hidden.decompose()
                yield url, (label or anchor).get_text(' ', strip=True)

    def courses(self):
        """Return {course id: name}, from the course service or dashboard links."""
        landing = self.visit(self.landing_url, rendered=True)
        landing_url = self.current_url
        session_key = self.session_key()
        courses = {}
        offset = 0
        try:
            while True:
                result = self.course_service(session_key, offset)
                batch = result['courses']
                if batch and all(str(course['id']) in courses for course in batch):
                    raise MoodleError('Course service repeated a page')
                next_offset = int(result['nextoffset'])
                for course in batch:
                    key = str(course['id'])
                    if re.fullmatch(r'[0-9]+', key):
                        courses[key] = (
                            clean(course.get('fullname') or course.get('shortname'))
                            or f'Module {key}'
                        )
                        self.module_aliases[key] = [
                            clean(course.get(field)) for field in ('shortname', 'idnumber')
                        ]
                if next_offset <= offset:
                    if batch:
                        raise MoodleError('Course service pagination did not advance')
                    break
                offset = next_offset
            self.stats['discovery'] = 'Moodle enrolled course service'
            return courses
        except SyncStopped:
            raise
        except Exception as error:
            self.issue('Course service', error)
        self.stats['discovery'] = 'Course links from dashboard'
        pages = [(landing, landing_url)]
        visited = {landing_url.rstrip('/')}
        for route in ('/my/courses.php', '/my/'):
            url = self.base + route
            if url.rstrip('/') in visited:
                continue
            try:
                pages.append((self.visit(url, rendered=True), self.current_url))
                visited.add(self.current_url.rstrip('/'))
            except SyncStopped:
                raise
            except Exception as error:
                self.issue('Dashboard fallback', error)
        for soup, url in pages:
            for href, label in self.links(soup, page_url=url):
                key = activity_id(href, self.base, COURSE)
                if key and label:
                    courses.setdefault(key, label)
        if not courses:
            raise MoodleError('No course links could be verified')
        return courses

    def calendar_events(self):
        """Read upcoming events from the Moodle calendar page."""
        try:
            page = self.visit(self.base + '/calendar/view.php?view=upcoming')
        except SyncStopped:
            raise
        except Exception as error:
            self.issue('Browser calendar', error)
            return []
        rows = []
        for item in page.select(
            '[data-region="event-item"], [data-region="event-list-item"], .event'
        ):
            self.check()
            start = item.select_one('time[datetime]')
            link = item.select_one('.event-name a[href], h3 a[href], a[href]')
            due = dateparser.parse(start['datetime']) if start else None
            if not (due and link):
                continue
            safe_path = calendar_link(link['href'])
            if not safe_path:
                continue
            href = urljoin(self.current_url, safe_path)
            if not self.allowed(href):
                continue
            title = link.get_text(' ', strip=True)
            rows.append(
                dict(
                    key=safe_path,
                    title=title,
                    due=due.date().isoformat(),
                    link=href,
                    category='Moodle calendar',
                    review=1,
                )
            )
        self.stats['browser_calendar'] = len(rows)
        return rows

    def grade_weights(self, module, assignments):
        """Fill missing weightings from the course's grade report."""
        if not any(not assignment['weighting'] for assignment in assignments):
            return
        try:
            page = self.visit(f'{self.base}/grade/report/user/index.php?id={module["id"]}')
        except SyncStopped:
            raise
        except Exception as error:
            self.issue('Student grade report', error, module['id'])
            return
        weights = {}
        for row in page.select('tr'):
            self.check()
            name = row.select_one('th.item.column-itemname')
            value = row.select_one('td.column-weight')
            if name and value:
                title = normalized_title(name.get_text(' ', strip=True))
                weights.setdefault(title, []).append(
                    weighting('Weighting: ' + value.get_text(' ', strip=True))
                )
        for assignment in assignments:
            title = normalized_title(assignment['title'])
            values = weights.get(title, [])
            if len(values) == 1 and not assignment['weighting']:
                assignment['weighting'] = values[0]

    def _collect_course_links(self, content, module, activities, sections=None):
        """Collect activity links, following section pages when needed."""
        links = list(self.links(content))
        for href, title in links:
            if sections is not None:
                section = activity_id(href, self.base, SECTION)
                if section:
                    sections.setdefault(section, href)
            activity = activity_id(href, self.base, ACTIVITY)
            if activity:
                activities.setdefault(activity, (href, title))
        self._scan_linked_pdfs(links, module)

    def _scan_course(self, module, assignments):
        """Scan one course page for assignments, resources and briefs."""
        key = module['id']
        try:
            soup = self.visit(f'{self.base}/course/view.php?id={key}')
        except SyncStopped:
            raise
        except Exception as error:
            self.issue('Module page', error, key)
            return
        self.bump('modules_scanned')
        activities = {}
        sections = {}
        self._collect_course_links(
            soup.select_one(MAIN_CONTENT) or soup, module, activities, sections
        )
        for section_id, link in sections.items():
            try:
                soup = self.visit(link)
                self._collect_course_links(
                    soup.select_one(MAIN_CONTENT) or soup, module, activities
                )
            except SyncStopped:
                raise
            except Exception as error:
                self.issue('Course section', error, section_id)
        for activity, (link, title) in activities.items():
            self.check()
            try:
                self._scan_activity(activity, link, title, module, assignments)
            except SyncStopped:
                raise
            except Exception as error:
                self.issue('Activity page', error, activity)

    def _scan_activity(self, activity, link, title, module, assignments):
        """Read one activity: an assignment page or a resource that may be a brief."""
        kind = ACTIVITY.search(urlsplit(link).path)[1]
        canonical = f'{self.base}/mod/{kind}/view.php?id={activity}'
        if kind == 'url' and not CA.search(title) and not title.lower().endswith(('.pdf', '.docx')):
            return
        if kind in ('resource', 'url'):
            self.pdf(canonical, title, module, resource=True)
            return
        page = self.visit(canonical)
        content = page.select_one(MAIN_CONTENT) or page
        if kind != 'assign':
            self.scan_links(content, module)
            return
        if activity_id(self.current_url, self.base, ASSIGN) != activity:
            raise MoodleError('Assignment page redirected to another activity')
        assignment = assignment_details(page, title)
        if assignment['due_unparsed']:
            self.issue(
                'Assignment deadline',
                MoodleError('Due date was present but could not be parsed'),
                activity,
            )
        assignment.update(
            key=f'assignment:{activity}',
            link=canonical,
            category=module['name'],
            module_id=module['id'],
        )
        assignments.append(assignment)
        self.stats['assignments'] = len(assignments)
        for href, label in self.links(content):
            parsed = urlsplit(href)
            if '/mod_assign/introattachment/' in parsed.path or any(
                '/mod_assign/introattachment/' in value
                for value in parse_qs(parsed.query).get('file', [])
            ):
                self.pdf(href, label, module, assignment)
        intro = content.select_one(INTRO)
        if intro is not None:
            self.scan_links(intro, module, assignment)
