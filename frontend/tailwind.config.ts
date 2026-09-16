import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        campus: {
          50: "#eef4ff",
          100: "#d9e6ff",
          500: "#3b6fd4",
          600: "#2f59ad",
          700: "#264887",
          900: "#16284b",
        },
      },
    },
  },
  plugins: [],
};

export default config;
