/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        ink: { 950: '#050b1a', 900: '#0a1326', 800: '#0f1b36', 700: '#16254a', 600: '#1f3260' },
        accent: { DEFAULT: '#3b82f6', soft: '#93c5fd' },
        brand: { 400: '#60a5fa' },
        buy: { DEFAULT: '#16c784', 400: '#4ade80' },
        sell: { DEFAULT: '#ea3943', 400: '#f87171' },
      },
      fontFamily: { sans: ['Inter', 'ui-sans-serif', 'system-ui', 'sans-serif'] },
    },
  },
  plugins: [],
}
