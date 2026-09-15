import { useState } from 'react';
import { api } from '../auth';
import { colors, space, t } from '../theme';

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

  const card = t.card();
  const inp = t.input();
  const btn = t.btnPrimary();

  return (
    <div
      style={{
        ...card.style, padding: space[6],
        maxWidth: 400, margin: '10vh auto 0',
      }}
      className={card.className}
    >
      <h1 style={{ fontSize: 20, marginBottom: 4, color: colors.ink, lineHeight: 1.3 }}>
        Zeffy-Workplace
      </h1>
      <p style={{ fontSize: 13, color: colors.gray, marginBottom: 20 }}>请登录后查看你的任务</p>
      <form onSubmit={submit} style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
        <input
          type="email" required value={email} onChange={(e) => setEmail(e.target.value)}
          placeholder="邮箱" autoComplete="username"
          style={inp.style} className={inp.className}
          data-state={error ? 'error' : undefined}
        />
        <input
          type="password" required value={password} onChange={(e) => setPassword(e.target.value)}
          placeholder="密码" autoComplete="current-password"
          style={inp.style} className={inp.className}
        />
        {error && (
          <div style={{ color: colors.danger, fontSize: 13 }} role="alert" aria-live="assertive">{error}</div>
        )}
        <button type="submit" disabled={busy} style={btn.style} className={btn.className}>
          {busy ? '登录中…' : '登录'}
        </button>
      </form>
    </div>
  );
}