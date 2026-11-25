import js from '@eslint/js';
import globals from 'globals';

export default [
  { ignores: ['node_modules/**', '.planner-dev/**', '**/assessment_pdfs/**'] },
  js.configs.recommended,
  {
    files: ['planner.js', 'static/**/*.js'],
    languageOptions: { globals: globals.browser },
  },
];
