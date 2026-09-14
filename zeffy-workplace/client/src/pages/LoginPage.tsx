import { useState } from 'react';
import { api } from '../auth';

export function LoginPage({ onLoggedIn }: { onLoggedIn: () => void }) {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!email.trim() || !password) return;
    setBusy(true);
    setError(null);
    try {
      const data = await api.login(email.trim(), password);
      localStorage.setItem('zw_token', data.token);
      onLoggedIn();
    } catch (err) {
      setError(err instanceof Error ? err.message : '登录失败');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div style={{ maxWidth: 400, margin: '10vh auto 0', padding: 24, border: '1px solid #e5e7eb', borderRadius: 8 }}>
      <h1 style={{ fontSize: 20, marginBottom: 4 }}>Zeffy-Workplace</h1>
      <p style={{ fontSize: 13, color: '#6b7280', marginBottom: 20 }}>请登录后查看你的任务</p>
      <form onSubmit={submit} style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
        <input
          type="email" required value={email} onChange={(e) => setEmail(e.target.value)}
          placeholder="邮箱" autoComplete="username"
          style={{ ...s.input, width: '100%', boxSizing: 'border-box' }}
        />
        <input
          type="password" required value={password} onChange={(e) => setPassword(e.target.value)}
          placeholder="密码" autoComplete="current-password"
          style={{ ...s.input, width: '100%', boxSizing: 'border-box' }}
        />
        {error && <div style={{ color: '#dc2626', fontSize: 13 }}>{error}</div>}
        <button type="submit" disabled={busy} style={s.btnPrimary}>
          {busy ? '登录中…' : '登录'}
        </button>
      </form>
    </div>
  );
}

const s: Record<string, React.CSSProperties> = {
  card: { border: '1px solid #e5e7eb', borderRadius: 8, padding: 12, background: '#fff' },
  input: { padding: 10, borderRadius: 6, border: '1px solid #d1d5db', fontSize: 14 },
  btnPrimary: { padding: '10px 16px', borderRadius: 6, border: 'none', background: '#2563eb', color: '#fff', cursor: 'pointer', fontSize: 14 },
};