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

import { useEffect, useState } from 'react';
import { fetchDashboardHealth, type DashboardHealth } from '../services/apiService';

export function useDashboardHealth() {
  const [health, setHealth] = useState<DashboardHealth | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    let active = true;

    const loadHealth = async () => {
      try {
        const status = await fetchDashboardHealth();
        if (active) {
          setHealth(status);
        }
      } catch (err) {
        if (active) {
          setError(err instanceof Error ? err.message : 'Failed to load dashboard health');
        }
      } finally {
        if (active) {
          setLoading(false);
        }
      }
    };

    loadHealth();

    return () => {
      active = false;
    };
  }, []);

  return {
    ready: Boolean(health?.ready),
    missing: health?.missing || [],
    source: health?.source,
    loading,
    error,
  };
}