import { useState } from 'react';
import { useZeffyWs } from './hooks/useZeffyWs';
import type { WsMessage } from './lib/protocol';

// P0：极简 echo 页面（不做 UI 美化，P4 统一做设计规范）。
function MessageRow({ msg }: { msg: WsMessage }) {
  const role = msg.kind === 'user_message' ? '你' : '系统';
  const content =
    typeof msg.payload === 'string' ? msg.payload : JSON.stringify(msg.payload);
  return (
    <div style={{ margin: '8px 0' }}>
      <strong>{role}:</strong> <span>{content}</span>
      <div style={{ fontSize: 12, color: '#888' }}>({msg.timestamp})</div>
    </div>
  );
}

export default function App() {
  const [input, setInput] = useState('');
  const ws = useZeffyWs({ url: `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws` });

  const handleSend = () => {
    const text = input.trim();
    if (!text) return;
    ws.send(text);
    setInput('');
  };

  return (
    <div style={{ maxWidth: 640, margin: '0 auto', padding: 24 }}>
      <h1>Zeffy-Workplace · P0 echo</h1>
      <div style={{ marginBottom: 8 }}>
        连接状态：<code>{ws.status}</code>
      </div>
      <div style={{ minHeight: 300, border: '1px solid #ddd', padding: 12 }}>
        {ws.messages.map((m) => (
          <MessageRow key={m.msg_id} msg={m} />
        ))}
      </div>
      <div style={{ marginTop: 16, display: 'flex', gap: 8 }}>
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && handleSend()}
          placeholder="输入 ping，回车发送"
          style={{ flex: 1, padding: 8 }}
        />
        <button onClick={handleSend} style={{ padding: '8px 16px' }}>
          发送
        </button>
      </div>
    </div>
  );
}