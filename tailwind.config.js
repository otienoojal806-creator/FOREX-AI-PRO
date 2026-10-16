/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        ink: { 950: '#040a1a', 900: '#08122a', 800: '#0d1a38', 700: '#14264d', 600: '#1d3566' },
        accent: { DEFAULT: '#3b82f6', soft: '#93c5fd' },
        brand: { 400: '#60a5fa', cyan: '#22d3ee' },
        buy: { DEFAULT: '#16c784', 400: '#4ade80' },
        sell: { DEFAULT: '#ea3943', 400: '#f87171' },
      },
      fontFamily: { sans: ['Inter', 'ui-sans-serif', 'system-ui', 'sans-serif'] },
      keyframes: {
        fadeUp: { '0%': { opacity: 0, transform: 'translateY(8px)' }, '100%': { opacity: 1, transform: 'none' } },
        shimmer: { '100%': { transform: 'translateX(100%)' } },
        toastIn: { '0%': { opacity: 0, transform: 'translateY(12px) scale(.98)' }, '100%': { opacity: 1, transform: 'none' } },
      },
      animation: {
        'fade-up': 'fadeUp .28s ease-out both',
        'toast-in': 'toastIn .22s ease-out both',
      },
    },
  },
  plugins: [],
}
