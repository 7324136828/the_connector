import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5130,
    host: true,
    proxy: {
      '/api': {
        target: `http://127.0.0.1:${process.env.CONNECTOR_BACKEND_PORT || '8301'}`,
        changeOrigin: true,
      },
    },
  },
});
