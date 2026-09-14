import { useCallback, useEffect, useState } from 'react';

/** 极简 hash 路由：`#/login`、`#/`、`#/tasks/:id`。不引 react-router（克制）。 */

export interface Route {
  name: 'login' | 'list' | 'detail' | 'notfound';
  params: Record<string, string>;
}

export function parseHash(hash: string): Route {
  const h = hash.replace(/^#/, '') || '/';
  const segs = h.split('/').filter(Boolean); // ['tasks',':id'] 或 []
  if (segs.length === 0) return { name: 'list', params: {} };
  if (segs[0] === 'login') return { name: 'login', params: {} };
  if (segs[0] === 'tasks') {
    if (segs[1]) return { name: 'detail', params: { id: decodeURIComponent(segs[1]) } };
    return { name: 'list', params: {} };
  }
  return { name: 'notfound', params: {} };
}

export function useHashRoute(): {
  route: Route;
  navigate: (to: string) => void;
} {
  const [hash, setHash] = useState<string>(() => location.hash || '#/');
  useEffect(() => {
    const onChange = () => setHash(location.hash || '#/');
    window.addEventListener('hashchange', onChange);
    return () => window.removeEventListener('hashchange', onChange);
  }, []);
  const navigate = useCallback((to: string) => {
    if (location.hash === to) {
      setHash(to);
      return;
    }
    location.hash = to;
  }, []);
  return { route: parseHash(hash), navigate };
}