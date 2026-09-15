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
  // ---- P6 治理（配额/回收站；治理未开启时后端 404 → 上层吞掉）----
  governanceStats: () => request<GovernanceStatsDTO>('/artifacts/stats', { cache: 'no-store' }),
  recycleList: () => request<{ items: RecycleItemDTO[]; total: number }>('/artifacts/recycle', { cache: 'no-store' }),
  recyclePost: (action: 'delete' | 'restore', taskId: string, rel: string) =>
    request<{ ok: boolean }>(`/artifacts/recycle/${encodeURIComponent(taskId)}/${encodeSegments(rel)}/${action}`, {
      method: 'POST',
    }),
  // ---- P6-2 运营体验（审计查询 + 批量操作）----
  quotaReport: () => request<QuotaReportDTO>('/artifacts/quota/report', { cache: 'no-store' }),
  governanceAudit: (params: { action?: string; taskId?: string; page?: number; pageSize?: number } = {}) =>
    request<AuditPageDTO>(`/artifacts/audit?${new URLSearchParams({
      ...(params.action ? { action: params.action } : {}),
      ...(params.taskId ? { task_id: params.taskId } : {}),
      page: String(params.page ?? 1),
      page_size: String(params.pageSize ?? 20),
    })}`, { cache: 'no-store' }),
  batchPost: (op: 'coldize' | 'delete' | 'restore', items: Array<{ task_id: string; rel_path: string }>) =>
    request<BatchResultDTO>(`/artifacts/batch/${op}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ items }),
    }),
  // ---- P6-4 运维状态（admin-only；非管理员 404 → 上层吞掉隐藏）----
  governanceAdminStatus: () => request<GovAdminStatusDTO>('/admin/governance/status', { cache: 'no-store' }),
  // P6-4-A 治理/系统告警历史（admin-only）
  governanceAdminAlerts: () =>
    request<GovAlertPageDTO>('/admin/governance/alerts?page=1&page_size=50', { cache: 'no-store' }),
  // P6-6-5 加密可观测状态（admin-only；白名单输出，越权 404 → 上层吞掉隐藏）
  encryptionStatus: () =>
    request<EncryptionStatusDTO>('/admin/governance/encryption-status', { cache: 'no-store' }),
  // P6-5 N3 容量规划（普通=本人，admin=全局+定价）
  storagePlan: () => request<StoragePlanDTO>('/storage/plan', { cache: 'no-store' }),
};

export interface GovernanceStatsDTO {
  count: number;
  total_bytes: number;
  hot_bytes: number;
  cold_bytes: number;
  owner_id: string;
  quota_total: number;
  quota_used: number;
}

export interface RecycleItemDTO {
  id: string;
  task_id: string | null;
  rel_path: string;
  size: number;
  tier: string;
  deleted_at: string | null;
}

export interface QuotaReportDTO {
  owner_id: string;
  quota_total: number;
  quota_used: number;
  trend: {
    slope_bytes_per_sec: number;
    eta_hours: number | null;
    trend: string;
    alert?: string | null;
  } | null;
  peak: { used_bytes: number; percent: number };
  suggestions: Array<{ task_id: string; rel_path: string; size: number; tier: string; status: string }>;
  cost: { period_days: number; hot: number; cold: number } | null;
  history_points: {
    points: Array<[number, number]>;
    start_ts: number | null;
    end_ts: number | null;
    downsampled: boolean;
  };
  cold_eligible: { logical_bytes: number; physical_bytes: number };
  suggested_quota: {
    quota: number; confidence: string; reason: string[];
    components: { hot: number; cold: number };
    cost_advice: { hint: string; cold_bytes: number; hot_bytes: number };
  } | null;
}

export interface DedupTraceDTO {
  sha256: string;
  refs: Array<{ task_id: string | null; rel_path: string; status: string; tier: string; size: number; owner_id: string | null }>;
}

export interface AuditItemDTO {
  id: string;
  operator: string;
  action: string;
  detail: Record<string, unknown> | null;
  task_id: string | null;
  created_at: string | null;
}

export interface AuditPageDTO {
  total: number;
  page: number;
  page_size: number;
  items: AuditItemDTO[];
}

export interface GovAdminFeatureDTO {
  name: string;
  config_attr: string;
  config_default: boolean;
  effective: boolean;
  overridden: boolean;
  gray_gated: boolean;
  gray_members: string[];
}

export interface GovAdminGuardianDTO {
  ts?: string | null;
  running?: boolean;
  ok?: boolean | null;
  error?: string;
}

export interface GovAdminStatusDTO {
  features: GovAdminFeatureDTO[];
  guardians: Record<string, GovAdminGuardianDTO>;
  gray: Record<string, string[]>;
  centralized?: boolean;
  store_backend?: string;
  degraded?: boolean;
  cache_version?: { ovr: Record<string, number>; gray: Record<string, number> };
  last_sync_ts?: string | null;
  single_instance_only: boolean;
  ts: string;
}

export interface GovAlertItemDTO {
  id: string;
  action: string;
  detail: {
    metric?: string;
    level?: string;
    threshold?: number | null;
    current?: string;
    dim?: string;
    description?: string;
    state?: string;
  };
  created_at: string | null;
}

export interface GovAlertPageDTO {
  total: number;
  page: number;
  page_size: number;
  items: GovAlertItemDTO[];
}

export interface StoragePlanDTO {
  enabled: boolean;
  owner_id?: string;
  period_days: number;
  tiers: { hot: { bytes: number; cost: number }; cold: { bytes: number; cost: number } };
  total_bytes: number;
  count: number;
  cost: { logical: number; physical: number; dedup_physical_bytes: number };
  reclaim: Array<{ rel_path: string; size: number; tier: string; status: string; saved_per_period: number }>;
  pricing?: { hot_per_gb: number; cold_per_gb: number };
}

// P6-6-5 加密可观测（admin 白名单；无任何密钥/密文/路径）
export interface EncryptionStatusDTO {
  enabled: boolean;
  key_loaded: boolean;
  algorithm: string | null;
  cipher_version: number | null;
  counters: { encrypt: number; decrypt: number; decrypt_fail: number; tamper: number;
              degrade_plain: number; unlock_fail: number; unlock_ok: number };
  window: { decrypt_fail: number; tamper: number; degrade_plain: number };
  decrypt_fail_rate: number;
  encrypted_physical_bytes: number;
  health_score: number;
  alarm_state: Record<string, string>;
}

export interface BatchResultDTO {
  op: string;
  succeeded: number;
  failed: number;
  impact: { count: number; bytes: number };
  items: Array<{ task_id: string; rel_path: string; ok: boolean; reason: string }>;
}