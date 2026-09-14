import { useEffect, useState } from 'react';
import { useZeffyWs } from './hooks/useZeffyWs';
import { useHashRoute } from './lib/router';
import { getToken } from './auth';
import { LoginPage } from './pages/LoginPage';
import { TaskListPage } from './pages/TaskListPage';
import { TaskDetailPage } from './pages/TaskDetailPage';

/**
 * 路由壳 + 认证态 + 全局单例 WS。
 * - hash 路由：`#/login`、`#/`（列表）、`#/tasks/:id`（详情）
 * - 全局单 WS：登录态建立一次连接(token)，页面间复用；详情按 taskId 分发事件
 * - 守卫：AUTH off 可直接进入；开启时接口 401 → 自动回登录页并清 token
 */
export default function App() {
  const { route, navigate } = useHashRoute();
  const [booted, setBooted] = useState(false);
  const [authed, setAuthed] = useState(false);

  const ws = useZeffyWs({
    url: `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`,
    token: getToken(),
  });

  // 启动探测：调 /tasks，401 → 未登录（AUTH on）；成功 → 匿名可用（AUTH off）
  useEffect(() => {
    (async () => {
      try {
        await fetch('/tasks', {
          headers: getToken() ? { Authorization: `Bearer ${getToken()}` } : {},
        });
        setAuthed(true);
      } catch {
        setAuthed(false);
      } finally {
        setBooted(true);
      }
    })();
  }, []);

  const handleLoggedIn = () => {
    setAuthed(true);
    navigate('#/');
  };
  const handleLoggedOut = () => {
    setAuthed(false);
    navigate('#/login');
  };

  if (!booted) {
    return <div style={{ padding: 40, color: '#6b7280' }}>加载中…</div>;
  }

  // 路由守卫：未登录（需登录时）→ 登录页
  if (!authed) {
    if (route.name === 'login') {
      return <LoginPage onLoggedIn={handleLoggedIn} />;
    }
    if (location.hash !== '#/login') navigate('#/login');
    return <LoginPage onLoggedIn={handleLoggedIn} />;
  }

  // 已登录：login 路由重定向到列表
  if (route.name === 'login') {
    navigate('#/');
    return null;
  }

  if (route.name === 'list') {
    return <TaskListPage onLoggedOut={handleLoggedOut} />;
  }

  if (route.name === 'detail') {
    return (
      <TaskDetailPage
        taskId={route.params.id}
        messages={ws.messages}
        wsStatus={ws.status}
        send={ws.send}
        sendDecision={ws.sendDecision}
        onBack={() => navigate('#/')}
        onAuthLost={handleLoggedOut}
      />
    );
  }

  return (
    <div style={{ padding: 40, color: '#6b7280' }}>
      页面不存在。 <button onClick={() => navigate('#/')}>回到任务列表</button>
    </div>
  );
}