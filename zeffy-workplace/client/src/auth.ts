// 前端鉴权会话 + 带 token 的 API 封装。
// token 存 localStorage；AUTH off 时后端不校验，匿名可访问。

const TOKEN_KEY = 'zw_token';

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string | null): void {
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY);
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

export interface TaskDTO {
  id: string;
  title: string;
  description: string;
  workflow_id: string;
  status: string;
  created_at: string;
}

export interface NodeDTO {
  id: string;
  node_name: string;
  status: string;
  error?: string | null;
  node_type?: string;
  output?: Record<string, unknown> | null;
}

export interface ArtifactListDTO {
  task_id: string;
  keys: string[];
  count: number;
}

/** P5 产物 key（artifacts/{task_id}/{rel}）→ 相对路径（去掉 task 前缀）。 */
export function artifactRel(key: string): string {
  return key.split('/').slice(2).join('/');
}

function encodeSegments(rel: string): string {
  return rel.split('/').map(encodeURIComponent).join('/');
}

async function request<T>(path: string, opts: RequestInit = {}): Promise<T> {
  const token = getToken();
  const headers: Record<string, string> = {
    ...(opts.headers as Record<string, string> | undefined),
  };
  if (token) headers.Authorization = `Bearer ${token}`;
  const res = await fetch(path, { ...opts, headers });
  if (res.status === 401) {
    clearToken();
    throw new ApiError(401, '未登录或会话过期');
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = (await res.json()) as { detail?: string };
      if (body.detail) detail = body.detail;
    } catch {
      // ignore JSON parse error
    }
    throw new ApiError(res.status, detail);
  }
  return res.json() as Promise<T>;
}

export const api = {
  login: (email: string, password: string) =>
    request<{ token: string; user: { id: string; email: string; username: string } }>('/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password }),
    }),
  logout: () => request<{ ok: boolean }>('/auth/logout', { method: 'POST' }),
  me: () => request<{ id: string; email: string; username: string }>('/auth/me'),
  listTasks: () => request<{ items: TaskDTO[]; total: number }>('/tasks', { cache: 'no-store' }),
  taskNodes: (taskId: string) =>
    request<{ items: NodeDTO[]; total: number }>(`/tasks/${taskId}/nodes`, { cache: 'no-store' }),
  // ---- P5 产物 ----
  artifactList: (taskId: string) =>
    request<ArtifactListDTO>(`/artifacts/${encodeURIComponent(taskId)}`, { cache: 'no-store' }),
  /** 下载产物内容（流式接口，返回 Blob；token 走 Authorization 头）。support AbortSignal. */
  artifactBlob: async (taskId: string, rel: string, signal?: AbortSignal): Promise<Blob> => {
    const token = getToken();
    const res = await fetch(`/artifacts/${encodeURIComponent(taskId)}/${encodeSegments(rel)}`, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
      signal,
    });
    if (res.status === 401) {
      clearToken();
      throw new ApiError(401, '未登录或会话过期');
    }
    if (!res.ok) {
      throw new ApiError(res.status, `产物读取失败(${res.status})`);
    }
    return res.blob();
  },
};