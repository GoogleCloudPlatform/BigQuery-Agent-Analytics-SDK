/*
 * Copyright 2026 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

import tailwindcss from '@tailwindcss/vite';
import react from '@vitejs/plugin-react';
import type { IncomingMessage, ServerResponse } from 'http';
import path from 'path';
import { defineConfig, type Plugin } from 'vite';
import { getDashboardRuntimeStatus, handleAgentDataRequest } from './api/agentData';

function localApiPlugin(): Plugin {
  return {
    name: 'local-api-proxy',
    configureServer(server) {
      server.middlewares.use(async (req: IncomingMessage & { method?: string }, res: ServerResponse, next) => {
        const url = req.url ? new URL(req.url, 'http://localhost') : null;
        if (!url || !url.pathname.startsWith('/api')) {
          next();
          return;
        }

        if (url.pathname === '/api/health') {
          res.statusCode = 200;
          res.setHeader('Content-Type', 'application/json; charset=utf-8');
          res.end(JSON.stringify(getDashboardRuntimeStatus()));
          return;
        }

        const result = await handleAgentDataRequest({
          method: req.method,
          headers: req.headers as Record<string, string | string[] | undefined>,
          query: Object.fromEntries(url.searchParams.entries()),
        });

        res.statusCode = result.status;
        res.setHeader('Content-Type', 'application/json; charset=utf-8');
        if (result.status === 405) {
          res.setHeader('Allow', 'GET');
        }
        res.end(JSON.stringify(result.body));
      });
    },
  };
}

export default defineConfig(() => {
  return {
    plugins: [react(), tailwindcss(), localApiPlugin()],
    base: './', // CRITICAL for Vercel deployment
    resolve: {
      alias: {
        // Change this to point to 'src' for cleaner imports
        '@': path.resolve(__dirname, './src'),
      },
    },
    build: {
      outDir: 'dist',
      assetsDir: 'assets',
      emptyOutDir: true, // Cleans the old build before making a new one
    },
    server: {
      hmr: process.env.DISABLE_HMR !== 'true',
    },
  };
});
