export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    extend: {
      colors: {
        // Deep neutral slate — a workshop instrument, not a void.
        base:  '#101215',
        panel: '#171A1F',
        raised:'#1E2229',
        line:  '#2A2F38',
        line2: '#383E49',
        ink:   '#E4E7EC',
        dim:   '#98A0AD',
        faint: '#616B7A',
        // Semantic only. These are HMI signal colours, not decoration.
        ok:    '#4ADE80',
        warn:  '#FBBF24',
        fault: '#F87171',
        live:  '#5EA9E8',
      },
      fontFamily: {
        sans: ['Inter', 'system-ui', '-apple-system', 'sans-serif'],
        mono: ['"JetBrains Mono"', 'ui-monospace', 'SFMono-Regular', 'Menlo', 'monospace'],
      },
      fontSize: {
        '2xs': ['0.6875rem', { lineHeight: '1rem' }],
      },
    },
  },
  plugins: [],
}
