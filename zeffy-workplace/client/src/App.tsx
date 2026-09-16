import { useEffect, useState } from 'react';
import { useZeffyWs } from './hooks/useZeffyWs';
import { useHashRoute } from './lib/router';
import { clearToken, getToken } from './auth';
import { api } from './auth';
import { LoginPage } from './pages/LoginPage';
import { TaskListPage } from './pages/TaskListPage';
import { TaskDetailPage } from './pages/TaskDetailPage';
import { WorkbenchNav } from './components/WorkbenchNav';
import { NewTaskModal } from './components/NewTaskModal';
import { HelpGuide } from './components/HelpGuide';
import { ToastRegion, toast } from './components/ui/Toast';

/**
 * 路由壳 + 认证态 + 全局单例 WS + 工作台导航 / 新建任务 / 使用指引。
 * - hash 路由：`#/login`、`#/`（工作台）、`#/tasks/:id`（详情）
 * - 守卫：AUTH off 可直接进入；开启时接口 401 → 自动回登录页并清 token
 */
export default function App() {
  const { route, navigate } = useHashRoute();
  const [booted, setBooted] = useState(false);
  const [authed, setAuthed] = useState(false);
  const [newTaskOpen, setNewTaskOpen] = useState(false);
  const [helpOpen, setHelpOpen] = useState(false);

  const ws = useZeffyWs({
    url: `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`,
    token: getToken(),
  });

  // 启动探测：调 /tasks，401 → 未登录（AUTH on,回登录页）；其余 → 登录态可用
  useEffect(() => {
    (async () => {
      try {
        const res = await fetch('/tasks', {
          headers: getToken() ? { Authorization: `Bearer ${getToken()}` } : {},
        });
        setAuthed(res.status !== 401);
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
    clearToken();
    setAuthed(false);
    navigate('#/login');
  };

  // 新建任务：经 WS 提交（不带 taskId → 后端新建并入队），随后定位到最新在跑任务详情
  const onCreate = async (prompt: string): Promise<void> => {
    ws.send(prompt);
    toast('任务已提交，正在进入任务详情…', 'ok');
    // 轮询 /tasks 取最新在跑任务；超时则回列表
    const deadline = Date.now() + 9000;
    const seen: Record<string, boolean> = {};
    let target: string | null = null;
    while (Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, 800));
      try {
        const data = await api.listTasks();
        for (const t of data.items) {
          if ((t.status === 'queued' || t.status === 'running' || t.status === 'pending') && !seen[t.id]) {
            seen[t.id] = true;
            target = t.id;
          }
        }
        if (target) break;
      } catch {
        /* 忽略轮询错误，超时兜底 */
      }
    }
    if (target) navigate(`#/tasks/${encodeURIComponent(target)}`);
    else navigate('#/');
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

  let content;
  if (route.name === 'list') {
    content = (
      <TaskListPage canCreate={authed} onNewTask={() => setNewTaskOpen(true)} onAuthLost={handleLoggedOut} />
    );
  } else if (route.name === 'detail' && route.params?.id) {
    content = (
      <TaskDetailPage
        taskId={route.params.id}
        messages={ws.messages}
        wsStatus={ws.status}
        sendDecision={ws.sendDecision}
        onBack={() => navigate('#/')}
        onNewTask={() => setNewTaskOpen(true)}
        onAuthLost={handleLoggedOut}
      />
    );
  } else {
    content = (
      <div style={{ padding: 40, color: '#6b7280' }}>
        页面不存在。 <button onClick={() => navigate('#/')}>回到工作台</button>
      </div>
    );
  }

  return (
    <>
      <WorkbenchNav
        wsStatus={ws.status}
        canCreate={authed}
        onNewTask={() => setNewTaskOpen(true)}
        onHelp={() => setHelpOpen(true)}
        onLogout={handleLoggedOut}
      />
      {content}
      <NewTaskModal open={newTaskOpen} onClose={() => setNewTaskOpen(false)} onCreate={onCreate} />
      <HelpGuide open={helpOpen} onClose={() => setHelpOpen(false)} />
      <ToastRegion />
    </>
  );
}