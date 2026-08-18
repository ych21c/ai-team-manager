"use client";
import { useEffect, useRef, useState, useCallback } from "react";
import type { CSSProperties } from "react";
import { reconcileApprovals } from "./reconcileApprovals";
import { formatMessageTime } from "./formatTime";
import { canDiscardStage, agentSubStatus } from "./flowchartRules";

// ── 타입 ──────────────────────────────────────────────────────────
type AgentStatus = "pending" | "running" | "completed" | "failed" | "waiting_approval";

interface AgentState {
  name: string; label: string; status: AgentStatus; progress: number; lastMessage: string;
}
interface StageOutputs {
  summary?: string; branch?: string; pr_number?: number; pr_url?: string;
  head_sha?: string; passed?: boolean; video_available?: boolean; agent?: string;
  design_preview?: boolean; design_summary?: string; architecture_summary?: string;
  input_tokens?: number; output_tokens?: number; cost_usd?: number;
  needs_rework?: boolean; feedback?: string;
}
interface StageInfo { status: string; agents: string[]; outputs?: StageOutputs; approved?: boolean; agents_done?: string[]; }
interface Message {
  id: string; from: string; content: string; ts: number; video?: string;
}
interface JiraInfo {
  epic?: string; stories?: string[]; confluence_url?: string; jira_url?: string;
}
interface TokenTotals { input_tokens: number; output_tokens: number; cost_usd: number; }
interface DeployConfig {
  app_name?: string; app_identifier?: string; language?: string; app_version?: string;
  environment?: "test" | "dev" | "prod"; platforms?: string[]; host_workspace_path?: string;
}
interface DeployStatus {
  status: "idle" | "running" | "success" | "failed";
  started_at?: number; finished_at?: number;
  app_version?: string; build_number?: string; log_tail?: string; error?: string;
}
interface Project {
  id: string; name: string; repo?: string;
  stages?: Record<string, StageInfo>;
  jira?: JiraInfo;
  token_totals?: TokenTotals;
  instruction?: string;
  sprint?: number;
  deploy_config?: DeployConfig;
  deploy_status?: DeployStatus;
}
interface ApprovalRequest {
  project_id: string; stage: string; message: string;
}
interface GithubRepo {
  full_name: string; name: string; private: boolean; updated_at: string; html_url: string;
}
interface OutputItem {
  type: string; label: string; icon: string; url: string; mtime: number;
}
interface DesignItem {
  key: string; title: string; url: string; mtime: number;
}
interface DesignHistoryItem {
  version: string; url: string; mtime: number;
}

function isProjectActive(p?: Project): boolean {
  if (!p?.stages) return false;
  return Object.values(p.stages).some(s => s.status === "running");
}

// 채팅 메시지 안의 링크를 실제 클릭 가능한 링크로 바꿔준다 — Jira/Confluence/
// 디자인 미리보기/녹화영상 링크를 대화창에 텍스트로만 찍어봐야 못 누르면 의미
// 없어서. 마크다운 [text](url), 절대 URL, "/design/xyz" 같은 상대
// 경로(같은 origin으로 풀어서 연결) 세 가지 형태를 다 처리한다.
function linkifyContent(text: string) {
  const pattern = /(\[[^\]]+\]\([^)]+\))|(https?:\/\/[^\s]+)|(\/(?:design|design-file|recordings|projects)\/[^\s]+)/g;
  const parts: (string | JSX.Element)[] = [];
  let lastIndex = 0;
  let key = 0;
  let m: RegExpExecArray | null;
  while ((m = pattern.exec(text)) !== null) {
    if (m.index > lastIndex) parts.push(text.slice(lastIndex, m.index));
    const token = m[0];
    if (token.startsWith("[")) {
      const mm = token.match(/^\[([^\]]+)\]\(([^)]+)\)$/);
      if (mm) {
        parts.push(<a key={key++} href={mm[2]} target="_blank" rel="noreferrer" style={{ color: "#7F77DD" }}>{mm[1]}</a>);
      } else {
        parts.push(token);
      }
    } else if (token.startsWith("http")) {
      parts.push(<a key={key++} href={token} target="_blank" rel="noreferrer" style={{ color: "#7F77DD", wordBreak: "break-all" }}>{token}</a>);
    } else {
      // 상대경로는 눈에 보이는 텍스트도 전체 URL로 보여준다 (경로만 보이면
      // 어디로 가는 링크인지 알아보기 어렵고 복사해서 쓰기도 불편함).
      const full = `${API_URL}${token}`;
      parts.push(<a key={key++} href={full} target="_blank" rel="noreferrer" style={{ color: "#7F77DD", wordBreak: "break-all" }}>{full}</a>);
    }
    lastIndex = m.index + token.length;
  }
  if (lastIndex < text.length) parts.push(text.slice(lastIndex));
  return parts;
}

const AGENT_META: Record<string, { label: string; color: string; bg: string }> = {
  pm:        { label: "PM",        color: "#0F6E56", bg: "#E1F5EE" },
  designer:  { label: "Designer",  color: "#854F0B", bg: "#FAEEDA" },
  architect: { label: "Architect", color: "#185FA5", bg: "#E6F1FB" },
  implement: { label: "Implement", color: "#3B6D11", bg: "#EAF3DE" },
  qa:        { label: "QA",        color: "#993556", bg: "#FBEAF0" },
  autotest:  { label: "AutoTest",  color: "#533489", bg: "#EEEDFE" },
  release:   { label: "Release",   color: "#5F5E5A", bg: "#F1EFE8" },
  user:      { label: "나",        color: "#534AB7", bg: "#EEEDFE" },
};

// 브라우저가 지금 접속한 origin(로컬이든 ngrok 터널이든) 기준으로 API/WS 주소를 잡는다.
// server.js가 "/"와 "/_next/*"를 뺀 모든 경로를 orchestrator로 프록시하기 때문에,
// 하드코딩된 localhost:8000을 쓰면 이 머신이 아닌 다른 기기(외부 터널 접속)에서
// API/WS 호출이 아예 나가지 못하는 문제가 있었다 — same-origin으로 고쳐서 해결.
const WS_URL  = typeof window !== "undefined"
  ? `${window.location.protocol === "https:" ? "wss:" : "ws:"}//${window.location.host}/ws`
  : "ws://localhost:8000/ws";
const API_URL = typeof window !== "undefined" ? window.location.origin : "http://localhost:8000";

const headerLinkStyle: CSSProperties = {
  fontSize: 12, color: "#888", textDecoration: "none",
  border: "0.5px solid #e5e5e5", padding: "4px 10px", borderRadius: 6, whiteSpace: "nowrap",
};

// Jira / Confluence / GitHub 링크를 흩어놓지 않고 버튼 하나(드롭다운)로 모아 보여준다.
// 프로젝트에 연결된 링크만 골라 보여주고, 하나도 없으면 버튼 자체를 숨긴다.
function ProjectLinksMenu({ project, variant = "header" }: { project?: Project; variant?: "header" | "mobile" | "compact" }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onClick = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, [open]);

  const links = [
    project?.jira?.jira_url && { label: "Jira", icon: "📋", url: project.jira.jira_url },
    project?.jira?.confluence_url && { label: "Confluence", icon: "📄", url: project.jira.confluence_url },
    project?.repo && { label: "GitHub", icon: "🔗", url: `https://github.com/${project.repo}` },
  ].filter(Boolean) as { label: string; icon: string; url: string }[];

  if (links.length === 0) return null;

  const buttonStyle: CSSProperties =
    variant === "compact"
      ? { background: "none", border: "none", color: "#bbb", fontSize: 12, lineHeight: 1, padding: 0, cursor: "pointer", display: "flex" }
      : variant === "mobile"
      ? { background: "none", border: "none", fontSize: 18, cursor: "pointer", color: "#555", padding: 4, lineHeight: 1 }
      : { ...headerLinkStyle, cursor: "pointer", background: "#fff" };

  return (
    <div ref={ref} style={{ position: "relative" }}>
      <button onClick={() => setOpen(o => !o)} title="프로젝트 링크 (Jira · Confluence · GitHub)" style={buttonStyle}>
        {variant === "header" ? "🔗 링크" : "🔗"}
      </button>
      {open && (
        <div style={{
          position: "absolute", top: "calc(100% + 4px)", right: 0, zIndex: 30,
          background: "#fff", border: "0.5px solid #e5e5e5", borderRadius: 8,
          boxShadow: "0 4px 16px rgba(0,0,0,0.08)", minWidth: 160, overflow: "hidden",
        }}>
          {links.map(l => (
            <a key={l.label} href={l.url} target="_blank" rel="noreferrer" onClick={() => setOpen(false)}
              style={{
                display: "flex", alignItems: "center", gap: 8, padding: "8px 12px",
                fontSize: 13, color: "#333", textDecoration: "none", whiteSpace: "nowrap",
              }}>
              <span>{l.icon}</span><span>{l.label}</span>
            </a>
          ))}
        </div>
      )}
    </div>
  );
}

const INITIAL_AGENTS = (): AgentState[] => [
  { name: "pm",        label: "PM",        status: "pending", progress: 0, lastMessage: "" },
  { name: "designer",  label: "Designer",  status: "pending", progress: 0, lastMessage: "" },
  { name: "architect", label: "Architect", status: "pending", progress: 0, lastMessage: "" },
  { name: "implement", label: "Implement", status: "pending", progress: 0, lastMessage: "" },
  { name: "qa",        label: "QA",        status: "pending", progress: 0, lastMessage: "" },
  { name: "autotest",  label: "AutoTest",  status: "pending", progress: 0, lastMessage: "" },
  { name: "release",   label: "Release",   status: "pending", progress: 0, lastMessage: "" },
];

// ── 상태 배지 ────────────────────────────────────────────────────
function StatusBadge({ status }: { status: AgentStatus }) {
  const map: Record<AgentStatus, [string, string]> = {
    pending: ["#F1EFE8","#5F5E5A"], running: ["#FAEEDA","#854F0B"],
    completed: ["#EAF3DE","#3B6D11"], failed: ["#FCEBEB","#A32D2D"],
    waiting_approval: ["#E6F1FB","#185FA5"],
  };
  const label: Record<AgentStatus, string> = {
    pending: "대기", running: "실행 중", completed: "완료",
    failed: "실패", waiting_approval: "승인 대기",
  };
  const [bg, color] = map[status];
  return <span style={{ background: bg, color, fontSize: 11, padding: "2px 8px", borderRadius: 20, whiteSpace: "nowrap" }}>{label[status]}</span>;
}

// ── 에이전트 실행 로그 뷰어 (DetailPanel + 플로우차트 노드 공용) ──────
// live=true면 2.5초마다 재조회(스테이지가 running일 때만 의미 있음), 아니면
// 클릭 시 1회만 조회 — 새 백엔드 스트리밍 없이 기존 GET /agent-log/{agent} 폴링만 추가.
function AgentLogView({ projectId, agent, live }: { projectId: string; agent: string; live: boolean }) {
  const [lines, setLines]     = useState<string[]>([]);
  const [shared, setShared]   = useState(false);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    const fetchLog = async () => {
      try {
        const r = await fetch(`${API_URL}/projects/${projectId}/agent-log/${agent}`);
        if (r.ok && !cancelled) {
          const d = await r.json();
          setLines(d.lines ?? []);
          setShared(!!d.shared);
        }
      } catch {} finally {
        if (!cancelled) setLoading(false);
      }
    };
    fetchLog();
    if (!live) return () => { cancelled = true; };
    const interval = setInterval(fetchLog, 2500);
    return () => { cancelled = true; clearInterval(interval); };
  }, [projectId, agent, live]);

  return (
    <div>
      <div style={{ fontSize: 11, fontWeight: 500, color: "#aaa", marginBottom: 8, display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <span>{AGENT_META[agent]?.label ?? agent} 로그{live && " · 실시간"}</span>
        {shared && <span style={{ color: "#bbb", fontWeight: 400 }}>공유 에이전트 — 다른 프로젝트 로그 섞일 수 있음</span>}
      </div>
      <pre style={{
        background: "#1a1a1a", color: "#ddd", fontSize: 10.5, lineHeight: 1.6, padding: 10, borderRadius: 8,
        maxHeight: 320, overflow: "auto", whiteSpace: "pre-wrap", wordBreak: "break-all", margin: 0,
      }}>
        {loading ? "불러오는 중..." : (lines.length ? lines.join("\n") : "로그 없음")}
      </pre>
    </div>
  );
}

// ── 상세 패널 (팀 현황 + 로그 + PR, 우측 슬라이드 오버레이) ──────────
function DetailPanel({ open, onClose, agents, projectId }: {
  open: boolean; onClose: () => void; agents: AgentState[]; projectId: string;
}) {
  const [detail, setDetail]         = useState<Project | null>(null);
  const [logAgent, setLogAgent]     = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setLogAgent(null);
    fetch(`${API_URL}/projects/${projectId}`)
      .then(r => (r.ok ? r.json() : null))
      .then(d => d && setDetail(d))
      .catch(() => {});
  }, [open, projectId]);

  const prEntries = Object.entries(detail?.stages ?? {}).filter(
    ([, s]) => s.outputs && (s.outputs.pr_url || s.outputs.branch)
  );

  return (
    <>
      {open && (
        <div onClick={onClose} style={{ position: "fixed", inset: 0, zIndex: 59, background: "rgba(0,0,0,0.25)" }} />
      )}
      <div style={{
        position: "fixed", top: 0, right: 0, bottom: 0, width: "min(400px, 100vw)",
        background: "#fff", borderLeft: "0.5px solid #e5e5e5", boxShadow: "-6px 0 24px rgba(0,0,0,0.1)",
        zIndex: 60, transform: open ? "translateX(0)" : "translateX(100%)", transition: "transform 0.22s ease",
        display: "flex", flexDirection: "column",
      }}>
        <div style={{ padding: "14px 16px", borderBottom: "0.5px solid #e5e5e5", display: "flex", justifyContent: "space-between", alignItems: "center", flexShrink: 0 }}>
          <span style={{ fontWeight: 500, fontSize: 14 }}>상세 정보</span>
          <button onClick={onClose} style={{ background: "none", border: "none", fontSize: 18, cursor: "pointer", color: "#888" }}>✕</button>
        </div>

        <div style={{ flex: 1, overflowY: "auto", padding: 14, display: "flex", flexDirection: "column", gap: 18 }}>
          {/* 팀 현황 */}
          <div>
            <div style={{ fontSize: 11, fontWeight: 500, color: "#aaa", marginBottom: 8 }}>팀 현황</div>
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              {agents.map(agent => {
                const meta = AGENT_META[agent.name] ?? { label: agent.name, color: "#666", bg: "#f0f0f0" };
                return (
                  <div key={agent.name} style={{ background: "#f9f8f5", border: "0.5px solid #e5e5e5", borderRadius: 8, padding: "9px 11px" }}>
                    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                      <span style={{ fontSize: 12.5, fontWeight: 500, display: "flex", alignItems: "center", gap: 6 }}>
                        <span style={{ width: 7, height: 7, borderRadius: "50%", background: meta.bg, border: `1.5px solid ${meta.color}`, display: "inline-block" }} />
                        {meta.label}
                      </span>
                      <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                        <StatusBadge status={agent.status} />
                        <button onClick={() => setLogAgent(agent.name)}
                          style={{ fontSize: 10.5, padding: "2px 6px", borderRadius: 4, border: "0.5px solid #d5d5d5", background: logAgent === agent.name ? "#EEEDFE" : "#fff", color: "#7F77DD", cursor: "pointer" }}>
                          로그
                        </button>
                      </div>
                    </div>
                    {agent.lastMessage && <div style={{ fontSize: 11, color: "#888", marginTop: 4, lineHeight: 1.5 }}>{agent.lastMessage}</div>}
                  </div>
                );
              })}
            </div>
          </div>

          {/* PR / 산출물 */}
          {prEntries.length > 0 && (
            <div>
              <div style={{ fontSize: 11, fontWeight: 500, color: "#aaa", marginBottom: 8 }}>PR / 산출물</div>
              <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                {prEntries.map(([stage, s]) => (
                  <div key={stage} style={{ fontSize: 12, background: "#f9f8f5", border: "0.5px solid #e5e5e5", borderRadius: 8, padding: "9px 11px" }}>
                    <div style={{ color: "#888", fontSize: 11, marginBottom: 3 }}>{stage}</div>
                    {s.outputs?.pr_url && (
                      <a href={s.outputs.pr_url} target="_blank" rel="noreferrer" style={{ color: "#7F77DD", wordBreak: "break-all" }}>{s.outputs.pr_url}</a>
                    )}
                    {!s.outputs?.pr_url && s.outputs?.branch && (
                      <div style={{ color: "#666", fontFamily: "monospace" }}>{s.outputs.branch}</div>
                    )}
                    {s.outputs?.summary && <div style={{ color: "#999", marginTop: 3 }}>{String(s.outputs.summary).slice(0, 200)}</div>}
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* 로그 뷰어 */}
          {logAgent && (
            <AgentLogView projectId={projectId} agent={logAgent}
              live={agents.find(a => a.name === logAgent)?.status === "running"} />
          )}
        </div>
      </div>
    </>
  );
}

// ── 산출물 패널 (디자인 목업/QA 녹화영상/스크린샷, 최신순, 우측 슬라이드) ──
function OutputsPanel({ open, onClose, projectId }: {
  open: boolean; onClose: () => void; projectId: string;
}) {
  const [outputs, setOutputs] = useState<OutputItem[]>([]);

  useEffect(() => {
    if (!open) return;
    fetch(`${API_URL}/outputs/${projectId}`)
      .then(r => (r.ok ? r.json() : null))
      .then(d => setOutputs(d?.items ?? []))
      .catch(() => setOutputs([]));
  }, [open, projectId]);

  return (
    <>
      {open && (
        <div onClick={onClose} style={{ position: "fixed", inset: 0, zIndex: 59, background: "rgba(0,0,0,0.25)" }} />
      )}
      <div style={{
        position: "fixed", top: 0, right: 0, bottom: 0, width: "min(360px, 100vw)",
        background: "#fff", borderLeft: "0.5px solid #e5e5e5", boxShadow: "-6px 0 24px rgba(0,0,0,0.1)",
        zIndex: 60, transform: open ? "translateX(0)" : "translateX(100%)", transition: "transform 0.22s ease",
        display: "flex", flexDirection: "column",
      }}>
        <div style={{ padding: "14px 16px", borderBottom: "0.5px solid #e5e5e5", display: "flex", justifyContent: "space-between", alignItems: "center", flexShrink: 0 }}>
          <span style={{ fontWeight: 500, fontSize: 14 }}>🗂 산출물 (최신순)</span>
          <button onClick={onClose} style={{ background: "none", border: "none", fontSize: 18, cursor: "pointer", color: "#888" }}>✕</button>
        </div>

        <div style={{ flex: 1, overflowY: "auto", padding: 14 }}>
          {outputs.length === 0 && (
            <div style={{ fontSize: 12.5, color: "#999", textAlign: "center", marginTop: 24 }}>
              아직 산출물이 없습니다 — 디자인 목업, QA 녹화영상, 스크린샷이 여기 최신순으로 나타납니다.
            </div>
          )}
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {outputs.map(item => (
              <a key={item.url} href={`${API_URL}${item.url}`} target="_blank" rel="noreferrer"
                style={{
                  display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8,
                  fontSize: 12.5, background: "#f9f8f5", border: "0.5px solid #e5e5e5", borderRadius: 8,
                  padding: "10px 12px", textDecoration: "none", color: "#333",
                }}>
                <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {item.icon} {item.label}
                </span>
                <span style={{ flexShrink: 0, fontSize: 11, color: "#7F77DD" }}>열기 ↗</span>
              </a>
            ))}
          </div>
        </div>
      </div>
    </>
  );
}

// 적용된 디자인 카드 — 클릭하면 그 시나리오의 과거 버전(머지 이력)을 펼쳐 보여준다.
function AppliedDesignCard({ item, projectId }: { item: DesignItem; projectId: string }) {
  const [historyOpen, setHistoryOpen] = useState(false);
  const [history, setHistory] = useState<DesignHistoryItem[] | null>(null);

  const toggleHistory = () => {
    const next = !historyOpen;
    setHistoryOpen(next);
    if (next && history === null) {
      fetch(`${API_URL}/design/${projectId}/history/${item.key}`)
        .then(r => (r.ok ? r.json() : null))
        .then(d => setHistory(d?.items ?? []))
        .catch(() => setHistory([]));
    }
  };

  return (
    <div style={{ background: "#f9f8f5", border: "0.5px solid #e5e5e5", borderRadius: 8, padding: "10px 12px" }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8, fontSize: 12.5 }}>
        <a href={`${API_URL}${item.url}`} target="_blank" rel="noreferrer"
          style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", textDecoration: "none", color: "#333", flex: 1 }}>
          🎨 {item.title}
        </a>
        <a href={`${API_URL}${item.url}`} target="_blank" rel="noreferrer" style={{ flexShrink: 0, fontSize: 11, color: "#7F77DD" }}>열기 ↗</a>
      </div>
      <button onClick={toggleHistory}
        style={{ marginTop: 6, background: "none", border: "none", color: "#aaa", fontSize: 11, cursor: "pointer", padding: 0 }}>
        {historyOpen ? "▾ 이전 버전 닫기" : "▸ 이전 버전"}
      </button>
      {historyOpen && (
        <div style={{ marginTop: 6, display: "flex", flexDirection: "column", gap: 4 }}>
          {history === null && <div style={{ fontSize: 11, color: "#bbb" }}>불러오는 중...</div>}
          {history?.length === 0 && <div style={{ fontSize: 11, color: "#bbb" }}>이전 버전 없음</div>}
          {history?.map(h => (
            <a key={h.version} href={`${API_URL}${h.url}`} target="_blank" rel="noreferrer"
              style={{ fontSize: 11, color: "#7F77DD", textDecoration: "none" }}>
              {new Date(h.mtime * 1000).toLocaleString()} 버전 ↗
            </a>
          ))}
        </div>
      )}
    </div>
  );
}

// ── 디자인 패널 (적용된 디자인 / 적용 전 새 디자인, 시나리오별, 우측 슬라이드) ──
function DesignPanel({ open, onClose, projectId }: {
  open: boolean; onClose: () => void; projectId: string;
}) {
  const [applied, setApplied] = useState<DesignItem[]>([]);
  const [pending, setPending] = useState<DesignItem[]>([]);

  useEffect(() => {
    if (!open) return;
    fetch(`${API_URL}/design/${projectId}/applied`)
      .then(r => (r.ok ? r.json() : null))
      .then(d => setApplied(d?.items ?? []))
      .catch(() => setApplied([]));
    fetch(`${API_URL}/design/${projectId}/pending`)
      .then(r => (r.ok ? r.json() : null))
      .then(d => setPending(d?.items ?? []))
      .catch(() => setPending([]));
  }, [open, projectId]);

  return (
    <>
      {open && (
        <div onClick={onClose} style={{ position: "fixed", inset: 0, zIndex: 59, background: "rgba(0,0,0,0.25)" }} />
      )}
      <div style={{
        position: "fixed", top: 0, right: 0, bottom: 0, width: "min(380px, 100vw)",
        background: "#fff", borderLeft: "0.5px solid #e5e5e5", boxShadow: "-6px 0 24px rgba(0,0,0,0.1)",
        zIndex: 60, transform: open ? "translateX(0)" : "translateX(100%)", transition: "transform 0.22s ease",
        display: "flex", flexDirection: "column",
      }}>
        <div style={{ padding: "14px 16px", borderBottom: "0.5px solid #e5e5e5", display: "flex", justifyContent: "space-between", alignItems: "center", flexShrink: 0 }}>
          <span style={{ fontWeight: 500, fontSize: 14 }}>🎨 디자인 (시나리오별)</span>
          <button onClick={onClose} style={{ background: "none", border: "none", fontSize: 18, cursor: "pointer", color: "#888" }}>✕</button>
        </div>

        <div style={{ flex: 1, overflowY: "auto", padding: 14, display: "flex", flexDirection: "column", gap: 18 }}>
          <div>
            <div style={{ fontSize: 11, fontWeight: 500, color: "#aaa", marginBottom: 8 }}>✅ 적용된 디자인</div>
            {applied.length === 0 && (
              <div style={{ fontSize: 12, color: "#bbb" }}>아직 머지된 디자인이 없습니다.</div>
            )}
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              {applied.map(item => (
                <AppliedDesignCard key={item.key} item={item} projectId={projectId} />
              ))}
            </div>
          </div>

          <div>
            <div style={{ fontSize: 11, fontWeight: 500, color: "#aaa", marginBottom: 8 }}>🆕 적용 전 (새 디자인)</div>
            {pending.length === 0 && (
              <div style={{ fontSize: 12, color: "#bbb" }}>적용 대기 중인 새 디자인이 없습니다.</div>
            )}
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              {pending.map(item => (
                <a key={item.key} href={`${API_URL}${item.url}`} target="_blank" rel="noreferrer"
                  style={{
                    display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8,
                    fontSize: 12.5, background: "#FAEEDA", border: "0.5px solid #F0D9AE", borderRadius: 8,
                    padding: "10px 12px", textDecoration: "none", color: "#333",
                  }}>
                  <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>🆕 {item.title}</span>
                  <span style={{ flexShrink: 0, fontSize: 11, color: "#854F0B" }}>열기 ↗</span>
                </a>
              ))}
            </div>
          </div>
        </div>
      </div>
    </>
  );
}

// ── 스프린트 플로우차트 탭 ────────────────────────────────────────────
// PM→Design→Implement→QA→AutoTest→Release 파이프라인을 순서도로 보여주고,
// 게이트(design/implement/autotest/release 시작 전)마다 No(입력 수정)/Run(재실행)/
// 폐기/Yes·Go(승인) 결정 블록을 둔다. implement→qa 사이는 게이트 없이 자동 진행.
const PIPELINE_ORDER = ["planning", "design", "implement", "qa", "autotest", "release"] as const;
type StageName = typeof PIPELINE_ORDER[number];
const STAGE_DEPENDS_ON: Partial<Record<StageName, StageName>> = {
  design: "planning", implement: "design", qa: "implement", autotest: "qa", release: "autotest",
};
// 이 스테이지들 앞에는 사람 승인 게이트가 있다(orchestrator/workflows/pipeline.py의
// requires_approval과 동일해야 함) — design/autotest는 이번에 새로 추가된 게이트,
// implement/release는 기존 게이트.
const GATED_STAGES = new Set<StageName>(["design", "implement", "autotest", "release"]);
const STAGE_META: Record<StageName, { label: string; agents: string[] }> = {
  planning:  { label: "PM",        agents: ["pm"] },
  design:    { label: "Design",    agents: ["designer", "architect"] },
  implement: { label: "Implement", agents: ["implement"] },
  qa:        { label: "QA",        agents: ["qa"] },
  autotest:  { label: "AutoTest",  agents: ["autotest"] },
  release:   { label: "Release",   agents: ["release"] },
};
// design 스테이지처럼 에이전트가 여럿인 곳에서, 각 에이전트가 자기 산출물을
// 어느 outputs 키에 남기는지 — orchestrator/agents/base/agent.py가 designer/
// architect를 서로 다른 키로 분리해서 쓰는 것과 짝을 맞춤(같은 "summary"에
// 같이 쓰면 나중에 끝나는 쪽이 먼저 쪽을 덮어써서 구분이 안 됨).
const AGENT_SUMMARY_KEY: Record<string, keyof StageOutputs> = {
  designer: "design_summary", architect: "architecture_summary",
};

// 서버 status만 보고 "지금 사람이 봐야 할 지점"을 순수 계산한다 — waiting_approval이든
// (정상 흐름) 폐기 직후의 pending+outputs 비어있음이든 이 함수 하나로 동일하게
// 처리되므로, 폐기가 "이전 스텝으로 돌아간" 것처럼 자연스럽게 보인다.
function getActiveGateStage(stages: Record<string, StageInfo> | undefined): StageName | null {
  if (!stages) return null;
  for (const name of PIPELINE_ORDER) {
    const s = stages[name];
    if (s?.status !== "completed") {
      const dep = STAGE_DEPENDS_ON[name];
      const depDone = !dep || stages[dep]?.status === "completed";
      return depDone ? name : null;
    }
  }
  return null; // 전부 완료
}

function gateIsPending(stages: Record<string, StageInfo> | undefined, gate: StageName): boolean {
  const s = stages?.[gate];
  if (!s) return false;
  if (s.status === "waiting_approval") return true;
  if (s.status === "pending") {
    const dep = STAGE_DEPENDS_ON[gate];
    return !dep || stages?.[dep]?.status === "completed";
  }
  return false;
}

function tokenBadgeText(outputs?: StageOutputs): string {
  if (outputs?.input_tokens === undefined && outputs?.output_tokens === undefined) return "—";
  const cost = outputs.cost_usd !== undefined ? ` · $${outputs.cost_usd.toFixed(4)}` : "";
  return `🔤 ${outputs.input_tokens ?? 0} in · ${outputs.output_tokens ?? 0} out${cost}`;
}

const flowBtnStyle: CSSProperties = {
  padding: "6px 12px", fontSize: 12, borderRadius: 6, cursor: "pointer", border: "0.5px solid #e5e5e5",
};

function DecisionBlock({ editTarget, gateTarget, stages, draft, setDraft, editing, setEditing, busy, onRun, onDiscard, onApprove }: {
  editTarget: StageName; gateTarget: StageName; stages: Record<string, StageInfo> | undefined;
  draft: Record<string, string>; setDraft: (fn: (d: Record<string, string>) => Record<string, string>) => void;
  editing: Record<string, boolean>; setEditing: (fn: (d: Record<string, boolean>) => Record<string, boolean>) => void;
  busy: Record<string, boolean>;
  onRun: (stage: string, feedback: string) => void;
  onDiscard: (stage: string) => void;
  onApprove: (stage: string, extraInput?: string) => void;
}) {
  const [extra, setExtra] = useState("");
  const isEditing = !!editing[editTarget];
  const editOutputs = stages?.[editTarget]?.outputs;

  if (isEditing) {
    return (
      <div style={{ margin: "2px 0 2px 26px", padding: "10px 12px", background: "#FAFAFA", border: "0.5px dashed #d5d5d5", borderRadius: 8, display: "flex", flexDirection: "column", gap: 8 }}>
        <div style={{ fontSize: 11, color: "#888" }}>'{STAGE_META[editTarget].label}' 다시 실행 — 수정 요청 내용을 적으세요</div>
        <textarea value={draft[editTarget] ?? ""} onChange={e => setDraft(d => ({ ...d, [editTarget]: e.target.value }))}
          rows={3} placeholder="예: 버튼 색상이 스펙과 달라요"
          style={{ width: "100%", padding: "8px 10px", border: "0.5px solid #d5d5d5", borderRadius: 6, fontSize: 12.5, fontFamily: "inherit", resize: "vertical", boxSizing: "border-box" }} />
        <div style={{ display: "flex", gap: 6 }}>
          <button onClick={() => setEditing(v => ({ ...v, [editTarget]: false }))}
            style={{ ...flowBtnStyle, background: "#f5f4f0", color: "#666" }}>취소</button>
          <button onClick={() => onRun(editTarget, draft[editTarget] ?? "")} disabled={busy[editTarget]}
            style={{ ...flowBtnStyle, background: "#7F77DD", border: "none", color: "#fff", fontWeight: 500 }}>
            {busy[editTarget] ? "실행 중..." : "▶ Run"}
          </button>
        </div>
      </div>
    );
  }

  return (
    <div style={{ margin: "2px 0 2px 26px", padding: "8px 12px", background: "#FAFAFA", border: "0.5px dashed #d5d5d5", borderRadius: 8, display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center" }}>
      <button onClick={() => {
        setEditing(v => ({ ...v, [editTarget]: true }));
        setDraft(d => ({ ...d, [editTarget]: editOutputs?.feedback ?? "" }));
      }} style={{ ...flowBtnStyle, background: "#fff", color: "#666" }}>No</button>
      <button onClick={() => onDiscard(gateTarget)} disabled={busy[gateTarget]}
        style={{ ...flowBtnStyle, background: "#FCEBEB", borderColor: "#F0C6C6", color: "#A32D2D" }}>폐기</button>
      <button onClick={() => onApprove(gateTarget)} disabled={busy[gateTarget]}
        style={{ ...flowBtnStyle, background: "#EAF3DE", borderColor: "#C0DD97", color: "#3B6D11", fontWeight: 500 }}>
        {busy[gateTarget] ? "..." : "✓ Yes"}
      </button>
      <input value={extra} onChange={e => setExtra(e.target.value)} placeholder="추가 요청(선택)"
        style={{ flex: 1, minWidth: 120, padding: "6px 8px", border: "0.5px solid #d5d5d5", borderRadius: 6, fontSize: 12 }} />
      <button onClick={() => onApprove(gateTarget, extra)} disabled={busy[gateTarget]}
        style={{ ...flowBtnStyle, background: "#F5F0FA", borderColor: "#D9C7EC", color: "#6B3FA0" }}>Go →</button>
    </div>
  );
}

function FlowNode({ name, stageInfo, projectId, isActive, expanded, onToggleExpand, onOpenDesign,
                    editing, onStartEdit, onCancelEdit, draft, setDraft, busy, onRun, onDiscard }: {
  name: StageName; stageInfo?: StageInfo; projectId: string; isActive: boolean;
  expanded: boolean; onToggleExpand: () => void; onOpenDesign: () => void;
  editing: Record<string, boolean>; onStartEdit: (stage: string) => void; onCancelEdit: (stage: string) => void;
  draft: Record<string, string>;
  setDraft: (fn: (d: Record<string, string>) => Record<string, string>) => void;
  busy: Record<string, boolean>; onRun: (stage: string, feedback: string) => void;
  onDiscard: (stage: string) => void;
}) {
  const meta = STAGE_META[name];
  const agentMeta = AGENT_META[meta.agents[0]] ?? { label: meta.label, color: "#666", bg: "#f0f0f0" };
  const status = (stageInfo?.status ?? "pending") as AgentStatus;
  const outputs = stageInfo?.outputs;
  // 완료된 스테이지는 게이트가 지나갔어도(혹은 애초에 게이트가 없어도 — qa처럼)
  // 노드에서 바로 같은 입력으로 재실행하거나, 산출물을 폐기하고 이전 단계로
  // 되돌릴 수 있어야 한다 — 게이트 결정 블록은 "지금 막 도달한 게이트"에서만
  // 잠깐 보이고 사라지므로 그것만으론 부족하다.
  const showInlineRerun = status === "completed";
  const canDiscard = canDiscardStage(name, status);
  const isInlineEditing = !!editing[name];

  const handleDiscard = () => {
    if (!window.confirm(`'${meta.label}' 산출물을 폐기하고 이전 단계로 되돌릴까요? 이후 단계 산출물도 함께 사라집니다.`)) return;
    onDiscard(name);
  };

  return (
    <div style={{
      background: "#f9f8f5", border: `0.5px solid ${isActive ? agentMeta.color : "#e5e5e5"}`,
      borderRadius: 8, padding: "10px 12px",
      boxShadow: isActive ? `0 0 0 2px ${agentMeta.bg}` : "none",
    }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", cursor: "pointer" }} onClick={onToggleExpand}>
        <span style={{ fontSize: 13, fontWeight: 500, display: "flex", alignItems: "center", gap: 7 }}>
          <span style={{ width: 8, height: 8, borderRadius: "50%", background: agentMeta.bg, border: `1.5px solid ${agentMeta.color}`, display: "inline-block" }} />
          {meta.label}
        </span>
        <span style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span style={{ fontSize: 10.5, color: "#aaa" }}>{tokenBadgeText(outputs)}</span>
          <StatusBadge status={status} />
          <span style={{ color: "#bbb", fontSize: 11 }}>{expanded ? "▲" : "▼"}</span>
        </span>
      </div>

      {expanded && (
        <div style={{ marginTop: 10, display: "flex", flexDirection: "column", gap: 8 }}>
          {name === "planning" ? null : (
            <div style={{ fontSize: 11.5, color: "#999" }}>
              게이트 통과 후 자동 시작 — 입력은 이전 스테이지 산출물
            </div>
          )}
          {meta.agents.length > 1 ? (
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              {meta.agents.map(a => {
                const aMeta = AGENT_META[a] ?? { label: a, color: "#666", bg: "#f0f0f0" };
                const aStatus = agentSubStatus(status, a, stageInfo?.agents_done) as AgentStatus;
                const aSummary = outputs?.[AGENT_SUMMARY_KEY[a] ?? "summary"];
                return (
                  <div key={a} style={{ background: "#fff", border: "0.5px solid #e5e5e5", borderRadius: 6, padding: "6px 8px" }}>
                    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                      <span style={{ fontSize: 11.5, fontWeight: 500, color: aMeta.color, display: "flex", alignItems: "center", gap: 5 }}>
                        <span style={{ width: 6, height: 6, borderRadius: "50%", background: aMeta.bg, border: `1.5px solid ${aMeta.color}`, display: "inline-block" }} />
                        {aMeta.label}
                      </span>
                      <StatusBadge status={aStatus} />
                    </div>
                    {aSummary && (
                      <div style={{ fontSize: 11.5, lineHeight: 1.5, color: "#555", marginTop: 4, maxHeight: 120, overflow: "auto", whiteSpace: "pre-wrap" }}>
                        {String(aSummary).slice(0, 1500)}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          ) : outputs?.summary && (
            <div style={{ fontSize: 12, lineHeight: 1.6, background: "#fff", border: "0.5px solid #e5e5e5", borderRadius: 6, padding: "8px 10px", maxHeight: 180, overflow: "auto", whiteSpace: "pre-wrap" }}>
              {String(outputs.summary).slice(0, 2000)}
            </div>
          )}
          {(outputs?.pr_url || outputs?.branch) && (
            <div style={{ fontSize: 11.5 }}>
              {outputs?.pr_url
                ? <a href={outputs.pr_url} target="_blank" rel="noreferrer" style={{ color: "#7F77DD" }}>{outputs.pr_url}</a>
                : <span style={{ fontFamily: "monospace", color: "#666" }}>{outputs?.branch}</span>}
            </div>
          )}
          {name === "design" && (
            <button onClick={onOpenDesign} style={{ ...flowBtnStyle, alignSelf: "flex-start", background: "#fff" }}>🎨 디자인 보기</button>
          )}
          {status === "running" && (
            <AgentLogView projectId={projectId} agent={meta.agents[0]} live />
          )}
          {showInlineRerun && (
            isInlineEditing ? (
              <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                <textarea value={draft[name] ?? ""} onChange={e => setDraft(d => ({ ...d, [name]: e.target.value }))}
                  rows={2} placeholder="재작업 요청 내용 (비워두면 같은 입력으로 재실행)" style={{ width: "100%", padding: "6px 8px", border: "0.5px solid #d5d5d5", borderRadius: 6, fontSize: 12, fontFamily: "inherit", boxSizing: "border-box" }} />
                <div style={{ display: "flex", gap: 6 }}>
                  <button onClick={() => onCancelEdit(name)}
                    style={{ ...flowBtnStyle, background: "#f5f4f0", color: "#666" }}>취소</button>
                  <button onClick={() => onRun(name, draft[name] ?? "")} disabled={busy[name]}
                    style={{ ...flowBtnStyle, background: "#7F77DD", border: "none", color: "#fff" }}>
                    {busy[name] ? "실행 중..." : "▶ Run"}
                  </button>
                </div>
              </div>
            ) : (
              <div style={{ display: "flex", gap: 6 }}>
                <button onClick={() => onStartEdit(name)}
                  style={{ ...flowBtnStyle, background: "#fff", color: "#666" }}>🔁 재실행</button>
                {canDiscard && (
                  <button onClick={handleDiscard} disabled={busy[name]}
                    style={{ ...flowBtnStyle, background: "#FCEBEB", borderColor: "#F0C6C6", color: "#A32D2D" }}>폐기</button>
                )}
              </div>
            )
          )}
        </div>
      )}
    </div>
  );
}

function FlowchartTab({ project, projectId, onOpenDesign }: {
  project: Project | undefined; projectId: string; onOpenDesign: () => void;
}) {
  const stages = project?.stages;
  const activeGate = getActiveGateStage(stages);
  const [draft, setDraft]     = useState<Record<string, string>>({});
  const [editing, setEditing] = useState<Record<string, boolean>>({});
  const [busy, setBusy]       = useState<Record<string, boolean>>({});
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [notice, setNotice]   = useState("");

  const isExpanded = (name: StageName): boolean => {
    if (expanded[name] !== undefined) return expanded[name];
    if (name === activeGate) return true;
    if (activeGate && STAGE_DEPENDS_ON[activeGate] === name) return true;
    return false;
  };

  const post = async (path: string, body?: unknown) => {
    const r = await fetch(`${API_URL}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body ?? {}),
    });
    if (!r.ok) {
      const text = await r.text().catch(() => "");
      throw new Error(text || `요청 실패 (${r.status})`);
    }
  };

  const withBusy = async (key: string, fn: () => Promise<void>) => {
    setBusy(b => ({ ...b, [key]: true }));
    setNotice("");
    try {
      await fn();
    } catch (e) {
      setNotice(e instanceof Error ? e.message : "요청 실패");
    } finally {
      setBusy(b => ({ ...b, [key]: false }));
    }
  };

  const runStage = (stage: string, feedback: string) => withBusy(stage, async () => {
    await post(`/projects/${projectId}/stage/${stage}/rerun`, { feedback });
    setEditing(v => ({ ...v, [stage]: false }));
  });
  const discardStage = (stage: string) => withBusy(stage, () => post(`/projects/${projectId}/stage/${stage}/discard`));
  const approveStage = (stage: string, extraInput?: string) => withBusy(stage, () =>
    post(`/projects/${projectId}/approve/${stage}`, extraInput ? { extra_input: extraInput } : {}));

  const totals = project?.token_totals;

  return (
    <div style={{ flex: 1, overflowY: "auto", padding: "16px 20px", display: "flex", flexDirection: "column", gap: 4 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
        <div style={{ fontSize: 11, color: "#aaa", display: "flex", alignItems: "center", gap: 8 }}>
          <span style={{
            fontSize: 10.5, fontWeight: 500, color: "#7F77DD", background: "#EEEDFE",
            border: "0.5px solid #DAD6F5", borderRadius: 5, padding: "1px 7px",
          }} title="planning부터 전체를 다시 돌린(재기획) 횟수">
            Sprint {project?.sprint ?? 1}
          </span>
          <span>PM → Design → Implement → QA → AutoTest → Release</span>
        </div>
        <div style={{ fontSize: 11, color: "#aaa" }}>
          누적: 🔤 {totals?.input_tokens ?? 0} in · {totals?.output_tokens ?? 0} out
          {totals?.cost_usd ? ` · $${totals.cost_usd.toFixed(4)}` : ""}
        </div>
      </div>

      {notice && (
        <div style={{ fontSize: 12, color: "#A32D2D", background: "#FCEBEB", border: "0.5px solid #F0C6C6", borderRadius: 6, padding: "6px 10px", marginBottom: 4 }}>
          {notice}
        </div>
      )}

      {PIPELINE_ORDER.map((name, i) => {
        const next = PIPELINE_ORDER[i + 1];
        const nextGated = next && GATED_STAGES.has(next);
        const showGate = nextGated && gateIsPending(stages, next);
        return (
          <div key={name}>
            <FlowNode
              name={name} stageInfo={stages?.[name]} projectId={projectId}
              isActive={name === activeGate || (nextGated && next === activeGate)}
              expanded={isExpanded(name)} onToggleExpand={() => setExpanded(v => ({ ...v, [name]: !isExpanded(name) }))}
              onOpenDesign={onOpenDesign}
              editing={editing} onStartEdit={n => setEditing(v => ({ ...v, [n]: true }))}
              onCancelEdit={n => setEditing(v => ({ ...v, [n]: false }))}
              draft={draft} setDraft={setDraft} busy={busy}
              onRun={runStage} onDiscard={discardStage}
            />
            {next && (
              showGate ? (
                <DecisionBlock
                  editTarget={name} gateTarget={next} stages={stages}
                  draft={draft} setDraft={setDraft} editing={editing} setEditing={setEditing} busy={busy}
                  onRun={runStage} onDiscard={discardStage} onApprove={approveStage}
                />
              ) : (
                <div style={{ margin: "2px 0 2px 26px", fontSize: 11, color: "#ccc", padding: "2px 0" }}>
                  {nextGated ? "↓" : "↓ 자동 진행"}
                </div>
              )
            )}
          </div>
        );
      })}

      <DeployPanel
        projectId={projectId}
        config={project?.deploy_config}
        status={project?.deploy_status}
        releaseCompleted={stages?.release?.status === "completed"}
      />
    </div>
  );
}

// ── 배포 카드 (스프린트 최하단) ──────────────────────────────────────
// Release 완료 후, 실제 앱스토어 빌드+업로드를 트리거하는 카드. 빌드 자체는
// Xcode가 필요해 이 웹/오케스트레이터가 도는 Docker 컨테이너 안에서는 못 돌고,
// 호스트에서 네이티브로 도는 scripts/deploy_runner.py가 대신 실행한다 —
// 자세한 흐름은 orchestrator/main.py의 POST /projects/{id}/deploy 참고.
const DEPLOY_FIELDS: { key: keyof DeployConfig; label: string; placeholder: string }[] = [
  { key: "app_name",       label: "App Name",       placeholder: "예: GoodEnough" },
  { key: "app_identifier", label: "App Identifier", placeholder: "예: com.myownchild.goodenough" },
  { key: "language",       label: "Language",       placeholder: "예: ko" },
  { key: "app_version",    label: "App Version",    placeholder: "예: 1.0.1" },
  { key: "host_workspace_path", label: "호스트 워크스페이스 경로", placeholder: "예: /Volumes/External/Dev/Development/child-care-medication" },
];

function DeployPanel({ projectId, config, status, releaseCompleted }: {
  projectId: string; config: DeployConfig | undefined; status: DeployStatus | undefined; releaseCompleted: boolean;
}) {
  const [editingField, setEditingField] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [logOpen, setLogOpen] = useState(false);

  const cfg = config ?? {};
  const st  = status ?? { status: "idle" as const };
  const platforms = cfg.platforms ?? ["ios", "android"];
  const environment = cfg.environment ?? "prod";

  const saveConfig = async (patch: Partial<DeployConfig>) => {
    setBusy(true);
    setNotice("");
    try {
      const r = await fetch(`${API_URL}/projects/${projectId}/deploy/config`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(patch),
      });
      if (!r.ok) throw new Error(await r.text().catch(() => "저장 실패"));
    } catch (e) {
      setNotice(e instanceof Error ? e.message : "저장 실패");
    } finally {
      setBusy(false);
    }
  };

  const startEdit = (key: string, current?: string) => { setEditingField(key); setDraft(current ?? ""); };
  const commitEdit = (key: string) => { setEditingField(null); void saveConfig({ [key]: draft }); };

  const triggerDeploy = async () => {
    setBusy(true);
    setNotice("");
    try {
      const r = await fetch(`${API_URL}/projects/${projectId}/deploy`, { method: "POST" });
      if (!r.ok) throw new Error(await r.text().catch(() => "배포 시작 실패"));
    } catch (e) {
      setNotice(e instanceof Error ? e.message : "배포 시작 실패");
    } finally {
      setBusy(false);
    }
  };

  const disabled = !releaseCompleted || st.status === "running";

  return (
    <div style={{
      marginTop: 14, padding: "14px 16px", borderRadius: 10,
      border: "0.5px solid #e5e5e5", background: releaseCompleted ? "#fff" : "#FAFAF8",
      opacity: releaseCompleted ? 1 : 0.6,
    }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
        <div style={{ fontSize: 13, fontWeight: 500, color: "#333" }}>🚀 배포</div>
        {!releaseCompleted && <div style={{ fontSize: 11, color: "#aaa" }}>Release 완료 후 배포 가능</div>}
      </div>

      {notice && (
        <div style={{ fontSize: 12, color: "#A32D2D", background: "#FCEBEB", border: "0.5px solid #F0C6C6", borderRadius: 6, padding: "6px 10px", marginBottom: 10 }}>
          {notice}
        </div>
      )}

      <div style={{ display: "flex", flexDirection: "column", gap: 6, marginBottom: 12 }}>
        {DEPLOY_FIELDS.map(({ key, label, placeholder }) => {
          const value = (cfg[key] as string | undefined) ?? "";
          const isEditing = editingField === key;
          return (
            <div key={key} style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12 }}>
              <div style={{ width: 150, flexShrink: 0, color: "#888" }}>{label}</div>
              {isEditing ? (
                <>
                  <input
                    autoFocus value={draft} placeholder={placeholder}
                    onChange={e => setDraft(e.target.value)}
                    onKeyDown={e => { if (e.key === "Enter") commitEdit(key); if (e.key === "Escape") setEditingField(null); }}
                    style={{ flex: 1, padding: "4px 8px", fontSize: 12, border: "0.5px solid #d5d5d5", borderRadius: 6, outline: "none" }}
                  />
                  <button onClick={() => commitEdit(key)} style={{ fontSize: 11, padding: "3px 8px", borderRadius: 5, border: "none", background: "#7F77DD", color: "#fff", cursor: "pointer" }}>저장</button>
                </>
              ) : (
                <>
                  <div style={{ flex: 1, color: value ? "#333" : "#bbb" }}>{value || "미설정"}</div>
                  <button onClick={() => startEdit(key, value)} title="수정" style={{ fontSize: 11, padding: "2px 6px", borderRadius: 5, border: "0.5px solid #e5e5e5", background: "#f7f7f5", color: "#888", cursor: "pointer" }}>✎</button>
                </>
              )}
            </div>
          );
        })}

        <div style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12 }}>
          <div style={{ width: 150, flexShrink: 0, color: "#888" }}>Environment</div>
          <select value={environment} onChange={e => void saveConfig({ environment: e.target.value as DeployConfig["environment"] })}
            style={{ fontSize: 12, padding: "4px 8px", border: "0.5px solid #d5d5d5", borderRadius: 6 }}>
            <option value="test">test</option>
            <option value="dev">dev</option>
            <option value="prod">prod</option>
          </select>
        </div>

        <div style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12 }}>
          <div style={{ width: 150, flexShrink: 0, color: "#888" }}>Platforms</div>
          {(["ios", "android"] as const).map(p => (
            <label key={p} style={{ display: "flex", alignItems: "center", gap: 4, color: "#555" }}>
              <input type="checkbox" checked={platforms.includes(p)}
                onChange={e => {
                  const next = e.target.checked ? [...platforms, p] : platforms.filter(x => x !== p);
                  void saveConfig({ platforms: next.length ? next : platforms });
                }}
              /> {p}
            </label>
          ))}
        </div>

        {st.build_number && (
          <div style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12 }}>
            <div style={{ width: 150, flexShrink: 0, color: "#888" }}>마지막 빌드번호</div>
            <div style={{ color: "#333" }}>{st.build_number} (자동 증가, 편집 불가)</div>
          </div>
        )}
      </div>

      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <button onClick={triggerDeploy} disabled={disabled || busy}
          style={{
            padding: "8px 16px", borderRadius: 8, border: "none", fontSize: 12.5, fontWeight: 500,
            background: disabled || busy ? "#e5e5e5" : "#0F6E56", color: disabled || busy ? "#999" : "#fff",
            cursor: disabled || busy ? "not-allowed" : "pointer",
          }}>
          {st.status === "running" ? "배포 중…" : "배포"}
        </button>
        {st.status === "success" && (
          <div style={{ fontSize: 12, color: "#3B6D11" }}>✅ {st.app_version}+{st.build_number} 배포 완료</div>
        )}
        {st.status === "failed" && (
          <>
            <div style={{ fontSize: 12, color: "#A32D2D" }}>❌ {st.error || "배포 실패"}</div>
            {st.log_tail && (
              <button onClick={() => setLogOpen(v => !v)} style={{ fontSize: 11, color: "#888", background: "none", border: "none", cursor: "pointer", textDecoration: "underline" }}>
                {logOpen ? "로그 숨기기" : "로그 보기"}
              </button>
            )}
          </>
        )}
      </div>

      {logOpen && st.log_tail && (
        <pre style={{ marginTop: 10, maxHeight: 240, overflow: "auto", fontSize: 11, background: "#1E1E1E", color: "#ddd", padding: 10, borderRadius: 6, whiteSpace: "pre-wrap" }}>
          {st.log_tail}
        </pre>
      )}
    </div>
  );
}

// ── 프로젝트 생성 모달 ────────────────────────────────────────────
function NewProjectModal({ onClose, onCreate }: {
  onClose: () => void;
  onCreate: (name: string, repo: string) => void;
}) {
  const [name, setName]   = useState("");
  const [repo, setRepo]   = useState("");
  const ref = useRef<HTMLInputElement>(null);
  useEffect(() => { ref.current?.focus(); }, []);
  const submit = () => { if (name.trim()) { onCreate(name.trim(), repo.trim()); onClose(); } };

  const inputStyle = { width: "100%", padding: "9px 12px", border: "0.5px solid #d5d5d5", borderRadius: 8, fontSize: 13, outline: "none", boxSizing: "border-box" as const };
  const labelStyle = { fontSize: 12, color: "#888", marginBottom: 4, display: "block" };

  return (
    <div style={{ position: "fixed", inset: 0, zIndex: 100, background: "rgba(0,0,0,0.35)", display: "flex", alignItems: "center", justifyContent: "center" }}>
      <div style={{ background: "#fff", borderRadius: 12, padding: "24px", width: 320, boxShadow: "0 8px 32px rgba(0,0,0,0.15)" }}>
        <div style={{ fontWeight: 500, fontSize: 15, marginBottom: 18 }}>새 프로젝트</div>

        <div style={{ marginBottom: 14 }}>
          <label style={labelStyle}>프로젝트 이름 *</label>
          <input ref={ref} value={name} onChange={e => setName(e.target.value)}
            onKeyDown={e => e.key === "Enter" && submit()}
            placeholder="예: 운동앱" style={inputStyle} />
        </div>

        <div style={{ marginBottom: 6 }}>
          <label style={labelStyle}>GitHub 레포지토리 (선택)</label>
          <input value={repo} onChange={e => setRepo(e.target.value)}
            placeholder="예: jayeun22/my-app" style={inputStyle} />
          <div style={{ marginTop: 5, fontSize: 11, color: "#aaa" }}>
            비우면 자동 생성 · 기존 레포 연결도 가능
          </div>
        </div>

        <div style={{ marginTop: 6, fontSize: 11, color: "#bbb", borderTop: "0.5px solid #f0f0f0", paddingTop: 10 }}>
          생성하면 이 프로젝트 전용 AI 팀이 자동으로 시작됩니다.
        </div>
        <div style={{ display: "flex", gap: 8, marginTop: 14 }}>
          <button onClick={onClose} style={{ flex: 1, padding: "9px", borderRadius: 8, border: "0.5px solid #e5e5e5", background: "#f5f4f0", color: "#666", fontSize: 13, cursor: "pointer" }}>취소</button>
          <button onClick={submit} style={{ flex: 1, padding: "9px", borderRadius: 8, border: "none", background: "#7F77DD", color: "#fff", fontSize: 13, cursor: "pointer", fontWeight: 500 }}>생성</button>
        </div>
      </div>
    </div>
  );
}

function RequirementModal({ onClose, onSubmit }: {
  onClose: () => void;
  onSubmit: (fullRewrite: boolean, feedback: string, jiraIssue: string | null) => void;
}) {
  const [fullRewrite, setFullRewrite] = useState(true);
  const [feedback, setFeedback]       = useState("");
  const [jiraIssue, setJiraIssue]     = useState("");
  const ref = useRef<HTMLTextAreaElement>(null);
  useEffect(() => { ref.current?.focus(); }, []);
  const submit = () => {
    if (!feedback.trim()) return;
    onSubmit(fullRewrite, feedback.trim(), fullRewrite ? null : (jiraIssue.trim() || null));
    onClose();
  };

  const inputStyle = { width: "100%", padding: "9px 12px", border: "0.5px solid #d5d5d5", borderRadius: 8, fontSize: 13, outline: "none", boxSizing: "border-box" as const };
  const labelStyle = { fontSize: 12, color: "#888", marginBottom: 4, display: "block" };

  return (
    <div style={{ position: "fixed", inset: 0, zIndex: 100, background: "rgba(0,0,0,0.35)", display: "flex", alignItems: "center", justifyContent: "center" }}>
      <div style={{ background: "#fff", borderRadius: 12, padding: "24px", width: 480, maxWidth: "90vw", boxShadow: "0 8px 32px rgba(0,0,0,0.15)" }}>
        <div style={{ fontWeight: 500, fontSize: 15, marginBottom: 4 }}>요구사항 변경</div>
        <div style={{ fontSize: 12, color: "#888", marginBottom: 16 }}>
          승인 대기 중인 스테이지가 있어도 요구사항이 바뀌었으면 PM 기획부터 다시 시작합니다.
        </div>

        <div style={{ display: "flex", gap: 8, marginBottom: 14 }}>
          <button onClick={() => setFullRewrite(true)}
            style={{
              flex: 1, padding: "8px", borderRadius: 8, cursor: "pointer", fontSize: 12,
              border: fullRewrite ? "1px solid #7F77DD" : "0.5px solid #e5e5e5",
              background: fullRewrite ? "#F1EFFC" : "#fff", color: fullRewrite ? "#4A3F9E" : "#666",
            }}>
            전체 요구사항 교체
          </button>
          <button onClick={() => setFullRewrite(false)}
            style={{
              flex: 1, padding: "8px", borderRadius: 8, cursor: "pointer", fontSize: 12,
              border: !fullRewrite ? "1px solid #7F77DD" : "0.5px solid #e5e5e5",
              background: !fullRewrite ? "#F1EFFC" : "#fff", color: !fullRewrite ? "#4A3F9E" : "#666",
            }}>
            기존 범위에 추가
          </button>
        </div>

        <div style={{ marginBottom: 14 }}>
          <label style={labelStyle}>{fullRewrite ? "새 전체 요구사항 *" : "이번에 반영할 요청사항 *"}</label>
          <textarea ref={ref} value={feedback} onChange={e => setFeedback(e.target.value)}
            placeholder={fullRewrite ? "새 전체 요구사항을 입력해주세요" : "이번에 반영할 요청사항을 입력해주세요"}
            rows={8}
            style={{ ...inputStyle, resize: "vertical", minHeight: 140, fontFamily: "inherit", lineHeight: 1.5 }} />
        </div>

        {!fullRewrite && (
          <div style={{ marginBottom: 6 }}>
            <label style={labelStyle}>관련 Jira 이슈 번호 (선택)</label>
            <input value={jiraIssue} onChange={e => setJiraIssue(e.target.value)}
              placeholder="예: PROJ-123" style={inputStyle} />
          </div>
        )}

        <div style={{ display: "flex", gap: 8, marginTop: 14 }}>
          <button onClick={onClose} style={{ flex: 1, padding: "9px", borderRadius: 8, border: "0.5px solid #e5e5e5", background: "#f5f4f0", color: "#666", fontSize: 13, cursor: "pointer" }}>취소</button>
          <button onClick={submit} disabled={!feedback.trim()}
            style={{ flex: 1, padding: "9px", borderRadius: 8, border: "none", background: feedback.trim() ? "#7F77DD" : "#ccc", color: "#fff", fontSize: 13, cursor: feedback.trim() ? "pointer" : "default", fontWeight: 500 }}>
            반영
          </button>
        </div>
      </div>
    </div>
  );
}

// ── 터널 배너 ────────────────────────────────────────────────────
function TunnelBanner() {
  const [url, setUrl] = useState("");
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    const poll = async () => {
      try {
        const r = await fetch(`${API_URL}/tunnel-urls`);
        if (r.ok) { const d = await r.json(); if (d.web) setUrl(d.web); }
      } catch {}
    };
    poll();
    const t = setInterval(poll, 8000);
    return () => clearInterval(t);
  }, []);
  if (!url) return null;
  return (
    <div style={{ background: "#EAF3DE", borderBottom: "0.5px solid #C0DD97", padding: "6px 14px", display: "flex", alignItems: "center", gap: 8, fontSize: 12 }}>
      <span style={{ color: "#3B6D11", fontWeight: 500, flexShrink: 0 }}>🌐</span>
      <a href={url} target="_blank" rel="noreferrer" style={{ color: "#0F6E56", fontFamily: "monospace", textDecoration: "none", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", flex: 1 }}>{url}</a>
      <button onClick={() => { navigator.clipboard.writeText(url); setCopied(true); setTimeout(() => setCopied(false), 2000); }}
        style={{ padding: "2px 8px", fontSize: 11, borderRadius: 4, border: "0.5px solid #C0DD97", background: "#fff", color: "#3B6D11", cursor: "pointer", flexShrink: 0 }}>
        {copied ? "✓" : "복사"}
      </button>
    </div>
  );
}

// ── 프로젝트 목록 (사이드바 내용) ───────────────────────────────
function ProjectList({ projects, activeId, onSelect, onCreate, connected, githubRepos, repoLoading, onConnectRepo, onRefreshRepos, onDeactivate, onActivate, onDiscard }: {
  projects: Project[]; activeId: string; onSelect: (id: string) => void;
  onCreate: () => void; connected: boolean;
  githubRepos: GithubRepo[]; repoLoading: boolean;
  onConnectRepo: (repo: GithubRepo) => void; onRefreshRepos: () => void;
  onDeactivate: (projectId: string) => void;
  onActivate: (projectId: string) => void;
  onDiscard: (projectId: string, name: string) => void;
}) {
  const sectionHeaderStyle = { padding: "12px 16px 6px", fontSize: 11, fontWeight: 500, color: "#aaa", display: "flex", alignItems: "center", justifyContent: "space-between" };
  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <div style={{ padding: "14px 16px", borderBottom: "0.5px solid #e5e5e5", display: "flex", alignItems: "center", justifyContent: "space-between" }}>
        <span style={{ fontSize: 12, fontWeight: 500, color: "#888" }}>프로젝트</span>
        <button onClick={onCreate}
          style={{ background: "none", border: "0.5px solid #d5d5d5", borderRadius: 6, padding: "3px 8px", fontSize: 18, cursor: "pointer", color: "#7F77DD", lineHeight: 1, display: "flex", alignItems: "center" }}
          title="새 프로젝트">+</button>
      </div>
      <div style={{ flex: 1, overflowY: "auto" }}>
        <div style={sectionHeaderStyle}>진행 중 프로젝트</div>
        {projects.map(p => {
          const active = isProjectActive(p);
          return (
            <div key={p.id} onClick={() => onSelect(p.id)}
              style={{ padding: "10px 16px", fontSize: 13, cursor: "pointer", display: "flex", alignItems: "center", gap: 7, borderLeft: p.id === activeId ? "3px solid #7F77DD" : "3px solid transparent", background: p.id === activeId ? "#fff" : "transparent", color: p.id === activeId ? "#1a1a1a" : "#555" }}>
              <span title={active ? "실행 중" : "대기 중 (다시 실행 가능)"}
                style={{ width: 6, height: 6, borderRadius: "50%", flexShrink: 0, background: active ? "#EF9F27" : "#ccc" }} />
              <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{p.name}</span>
              {active ? (
                <button onClick={e => { e.stopPropagation(); onDeactivate(p.id); }} title="인액티브로 전환 (일시정지, 나중에 대화로 재개 가능)"
                  style={{ flexShrink: 0, border: "0.5px solid #e5c5c5", background: "#fff", color: "#c07070", borderRadius: 4, fontSize: 10, padding: "1px 5px", cursor: "pointer", lineHeight: 1.4 }}>
                  정지
                </button>
              ) : (
                <button onClick={e => { e.stopPropagation(); onActivate(p.id); }} title="팀을 다시 기동해서 이어서 실행"
                  style={{ flexShrink: 0, border: "0.5px solid #c5d5c8", background: "#fff", color: "#3f8f5c", borderRadius: 4, fontSize: 10, padding: "1px 5px", cursor: "pointer", lineHeight: 1.4 }}>
                  재실행
                </button>
              )}
              <button onClick={e => { e.stopPropagation(); onDiscard(p.id, p.name); }} title="프로젝트 폐기 (되돌릴 수 없음)"
                style={{ flexShrink: 0, border: "0.5px solid #F0C6C6", background: "#fff", color: "#A32D2D", borderRadius: 4, fontSize: 10, padding: "1px 5px", cursor: "pointer", lineHeight: 1.4 }}>
                폐기
              </button>
              <div onClick={e => e.stopPropagation()} style={{ flexShrink: 0 }}>
                <ProjectLinksMenu project={p} variant="compact" />
              </div>
            </div>
          );
        })}

        <div style={sectionHeaderStyle}>
          <span>내 GitHub 레포</span>
          <button onClick={onRefreshRepos} title="새로고침"
            style={{ background: "none", border: "none", color: "#aaa", cursor: "pointer", fontSize: 12, padding: 2 }}>↻</button>
        </div>
        {repoLoading && <div style={{ padding: "4px 16px 10px", fontSize: 12, color: "#bbb" }}>불러오는 중...</div>}
        {!repoLoading && githubRepos.length === 0 && (
          <div style={{ padding: "4px 16px 10px", fontSize: 12, color: "#bbb" }}>연결 안 된 레포 없음</div>
        )}
        {githubRepos.map(r => (
          <div key={r.full_name} onClick={() => onConnectRepo(r)}
            style={{ padding: "8px 16px", fontSize: 12.5, cursor: "pointer", color: "#666", display: "flex", alignItems: "center", gap: 6 }}
            title={`클릭해서 ${r.full_name} 연결`}>
            <span style={{ color: "#bbb" }}>{r.private ? "🔒" : "📦"}</span>
            <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{r.name}</span>
          </div>
        ))}
        <div style={{ height: 8 }} />
      </div>
      <div style={{ padding: "10px 14px", borderTop: "0.5px solid #e5e5e5", fontSize: 11, color: connected ? "#1D9E75" : "#E24B4A", display: "flex", alignItems: "center", gap: 5 }}>
        <span style={{ width: 6, height: 6, borderRadius: "50%", background: connected ? "#1D9E75" : "#E24B4A", display: "inline-block" }} />
        {connected ? "연결됨" : "연결 중..."}
      </div>
    </div>
  );
}

// ── 메인 ─────────────────────────────────────────────────────────
export default function Home() {
  const [projects, setProjects]       = useState<Project[]>([{ id: "web-comm", name: "웹 커뮤니케이션 프로젝트" }]);
  const [activeId, setActiveId]       = useState("web-comm");
  // 프로젝트별 메시지/에이전트 상태 맵
  const [msgMap, setMsgMap]           = useState<Record<string, Message[]>>({ "web-comm": [] });
  const [agentMap, setAgentMap]       = useState<Record<string, AgentState[]>>({ "web-comm": INITIAL_AGENTS() });
  const [approvals, setApprovals]     = useState<ApprovalRequest[]>([]);
  const [input, setInput]             = useState("");
  const [connected, setConnected]     = useState(false);
  const [drawerOpen, setDrawerOpen]   = useState(false);
  const [showModal, setShowModal]     = useState(false);
  const [requirementReq, setRequirementReq] = useState<ApprovalRequest | null>(null);
  const [detailOpen, setDetailOpen]   = useState(false);
  const [outputsOpen, setOutputsOpen] = useState(false);
  const [designOpen, setDesignOpen]   = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [githubRepos, setGithubRepos] = useState<GithubRepo[]>([]);
  const [repoLoading, setRepoLoading] = useState(false);
  // 플로우차트 탭이 기본값 — 사용자가 "첫번째 탭"으로 요청한 스프린트 흐름을 먼저 보여줌
  const [activeTab, setActiveTab]     = useState<"flow" | "chat">("flow");
  const wsRef    = useRef<WebSocket | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const saved = localStorage.getItem("sidebarCollapsed");
    if (saved) setSidebarCollapsed(saved === "1");
  }, []);
  const toggleSidebar = () => {
    setSidebarCollapsed(prev => {
      localStorage.setItem("sidebarCollapsed", prev ? "0" : "1");
      return !prev;
    });
  };

  const fetchGithubRepos = useCallback(async () => {
    setRepoLoading(true);
    try {
      const r = await fetch(`${API_URL}/github/repos`);
      if (r.ok) setGithubRepos(await r.json());
    } catch {} finally { setRepoLoading(false); }
  }, []);
  useEffect(() => { fetchGithubRepos(); }, [fetchGithubRepos]);

  // 현재 프로젝트의 메시지/에이전트
  const messages = msgMap[activeId] ?? [];
  const agents   = agentMap[activeId] ?? INITIAL_AGENTS();
  const activeProject = projects.find(p => p.id === activeId);
  const runningCount  = agents.filter(a => a.status === "running").length;

  // 헤더의 🎨 디자인 버튼은 DesignPanel이 열릴 때 /design/{id}/applied,pending을
  // 직접 fetch하므로(OutputsPanel/DetailPanel과 동일 패턴) 여기서 미리 확인할 필요가 없다.

  // 프로젝트 전환(사이드바 클릭)이나 WS 재연결로 이력이 한 번에 복원되는 init
  // 이벤트 때는 addMessage 쪽 스크롤(새 메시지 도착 시에만 동작)이 안 불려서
  // 이전 스크롤 위치가 그대로 남는 문제 — 대화가 항상 최신 메시지로 열리도록
  // activeId가 바뀌거나 메시지가 처음 채워질 때 바닥으로 보낸다.
  useEffect(() => {
    if (messages.length) bottomRef.current?.scrollIntoView({ behavior: "auto" });
  }, [activeId, messages.length]);

  // 가장 최근에 대화한 프로젝트가 맨 위로
  const lastActivity = useCallback((id: string) => {
    const arr = msgMap[id];
    return arr && arr.length ? arr[arr.length - 1].ts : 0;
  }, [msgMap]);
  const sortedProjects = [...projects].sort((a, b) => lastActivity(b.id) - lastActivity(a.id));

  const addMessage = useCallback((projectId: string, from: string, content: string) => {
    setMsgMap(prev => ({
      ...prev,
      [projectId]: [...(prev[projectId] ?? []), { id: Math.random().toString(36).slice(2), from, content, ts: Date.now() }],
    }));
    if (projectId === activeId) {
      setTimeout(() => bottomRef.current?.scrollIntoView({ behavior: "smooth" }), 50);
    }
  }, [activeId]);

  const addVideoMessage = useCallback((projectId: string, from: string, content: string, video: string) => {
    setMsgMap(prev => ({
      ...prev,
      [projectId]: [...(prev[projectId] ?? []), { id: Math.random().toString(36).slice(2), from, content, ts: Date.now(), video }],
    }));
    if (projectId === activeId) {
      setTimeout(() => bottomRef.current?.scrollIntoView({ behavior: "smooth" }), 50);
    }
  }, [activeId]);

  // WebSocket
  useEffect(() => {
    const connect = () => {
      const ws = new WebSocket(WS_URL);
      wsRef.current = ws;
      ws.onopen = () => setConnected(true);
      ws.onclose = () => { setConnected(false); setTimeout(connect, 2000); };
      ws.onmessage = (e) => handleEvent(JSON.parse(e.data));
    };
    connect();
    return () => wsRef.current?.close();
  }, []);

  const handleEvent = useCallback((event: Record<string, unknown>) => {
    const type = event.type as string;
    const pid  = event.project_id as string;

    if (type === "init") {
      const raw = event.projects as Record<string, Project & { messages?: Message[] }>;
      if (raw) {
        const list = Object.entries(raw).map(([id, v]) => ({
          id, name: v.name ?? id, repo: v.repo, stages: v.stages, jira: v.jira, sprint: v.sprint,
          deploy_config: v.deploy_config, deploy_status: v.deploy_status,
        }));
        setProjects(list);
        // approval_required는 단발성 이벤트라 그 순간 연결이 안 돼 있었으면
        // 놓친다 — 서버 상태(stages[].status)를 기준으로 승인 목록을 다시 맞춘다.
        setApprovals(prev => reconcileApprovals(prev, list));
        const ids = list.map(p => p.id);
        // 서버가 들고 있는 채팅 이력으로 복원 (새로고침으로 WS가 재연결돼도
        // 대화가 안 사라지게) — 서버 쪽에 기록이 없으면 기존 로컬 상태 유지.
        setMsgMap(prev => Object.fromEntries(ids.map(id => [id, raw[id]?.messages ?? prev[id] ?? []])));
        setAgentMap(prev => Object.fromEntries(ids.map(id => [id, prev[id] ?? INITIAL_AGENTS()])));
      }
    }

    else if (type === "project_added" || type === "project_updated") {
      const raw = event.projects as Record<string, Project>;
      if (raw) {
        const updates = Object.entries(raw).map(([id, v]) => ({
          id, name: v.name ?? id, repo: v.repo, stages: v.stages, jira: v.jira, sprint: v.sprint,
          deploy_config: v.deploy_config, deploy_status: v.deploy_status,
        }));
        // project_updated는 바뀐 프로젝트 하나만 실어 보낸다(discard/approve/deploy
        // 등 대부분의 엔드포인트가 그렇게 브로드캐스트함) — 예전엔 여기서 setProjects를
        // raw 그대로 통째로 교체해버려서, 다른 프로젝트를 사이드바/승인 목록에서
        // 순간적으로 지워버리는 버그가 있었다(배포 카드 저장을 테스트하다 실제로
        // 재현: 프로젝트 3개 중 1개만 저장했는데 사이드바에 1개만 남음). prev와
        // merge해서 언급 안 된 프로젝트는 그대로 둔다.
        let list: Project[] = [];
        setProjects(prev => {
          const byId = new Map(prev.map(p => [p.id, p]));
          updates.forEach(u => byId.set(u.id, { ...byId.get(u.id), ...u }));
          list = Array.from(byId.values());
          return list;
        });
        // 여기서도 마찬가지로 서버 상태 기준으로 승인 목록을 다시 맞춘다 — design
        // 재작업 등으로 stage가 waiting_approval로 바뀌었는데 그 사이 WS가
        // 끊겼다 재연결됐으면 approval_required 이벤트를 못 받을 수 있다.
        setApprovals(prev => reconcileApprovals(prev, list));
        setGithubRepos(prev => prev.filter(r => !list.some(p => p.repo === r.full_name)));
        setMsgMap(prev => {
          const next = { ...prev };
          list.forEach(p => { if (!next[p.id]) next[p.id] = []; });
          return next;
        });
        setAgentMap(prev => {
          const next = { ...prev };
          list.forEach(p => { if (!next[p.id]) next[p.id] = INITIAL_AGENTS(); });
          return next;
        });
        // 새 프로젝트로 자동 전환
        if (type === "project_added" && pid) setActiveId(pid);
      }
    }

    else if (type === "project_removed") {
      setProjects(prev => prev.filter(p => p.id !== pid));
      setActiveId(prev => prev === pid ? "web-comm" : prev);
    }

    else if (type === "agent_message") {
      addMessage(pid, event.agent as string, event.content as string);
      setAgentMap(prev => ({
        ...prev,
        [pid]: (prev[pid] ?? INITIAL_AGENTS()).map(a =>
          a.name === event.agent ? { ...a, lastMessage: (event.content as string).slice(0, 80) } : a
        ),
      }));
    }

    else if (type === "stage_update") {
      const stageMap: Record<string, string[]> = {
        planning: ["pm"], design: ["designer", "architect"],
        implement: ["implement"], qa: ["qa"], autotest: ["autotest"], release: ["release"],
      };
      const affected = stageMap[event.stage as string] ?? [];
      const status   = event.status as AgentStatus;
      setAgentMap(prev => ({
        ...prev,
        [pid]: (prev[pid] ?? INITIAL_AGENTS()).map(a =>
          affected.includes(a.name) ? { ...a, status, progress: status === "completed" ? 100 : 10 } : a
        ),
      }));
      setProjects(prev => prev.map(p => p.id !== pid ? p : {
        ...p,
        stages: { ...(p.stages ?? {}), [event.stage as string]: { status, agents: affected } },
      }));

      // QA 완료 → 녹화 영상이 있으면 PM 메시지처럼 대화 흐름에 인라인으로 삽입
      if (event.stage === "qa" && status === "completed") {
        const videoUrl = `${API_URL}/recordings/${pid}`;
        fetch(videoUrl, { method: "HEAD" })
          .then(r => { if (r.ok) addVideoMessage(pid, "qa", "🎥 QA 테스트 녹화 영상", videoUrl); })
          .catch(() => {});
      }
    }

    else if (type === "agent_progress") {
      setAgentMap(prev => ({
        ...prev,
        [pid]: (prev[pid] ?? INITIAL_AGENTS()).map(a =>
          a.name === event.agent ? { ...a, progress: event.progress as number, lastMessage: (event.message as string).slice(0, 80) } : a
        ),
      }));
    }

    else if (type === "approval_required") {
      const req = event as unknown as ApprovalRequest;
      // 같은 (프로젝트, 스테이지) 승인 요청이 중복으로 오면(예: 개발 모드 WS
      // 재연결 등) 카드가 두 개씩 뜨는 걸 막는다.
      setApprovals(prev =>
        prev.some(a => a.project_id === req.project_id && a.stage === req.stage) ? prev : [...prev, req]
      );
      // 여러 프로젝트를 동시에 돌리다 게이트 열린 걸 놓치기 쉬워서, 탭이
      // 백그라운드일 때 타이틀에 대기 개수를 띄우고 브라우저 알림도 시도한다.
      if (document.hidden && typeof Notification !== "undefined") {
        if (Notification.permission === "default") Notification.requestPermission().catch(() => {});
        if (Notification.permission === "granted") {
          try {
            new Notification("AI 개발팀 — 승인 대기", { body: req.message || `'${req.stage}' 게이트 승인 필요` });
          } catch {}
        }
      }
    }
  }, [addMessage, addVideoMessage]);

  // 대기 중인 게이트 개수를 탭 타이틀에 표시 — 백그라운드 탭에서도 한눈에 보이게.
  useEffect(() => {
    const pending = approvals.length;
    document.title = pending > 0 ? `(${pending}) AI 개발팀` : "AI 개발팀";
  }, [approvals.length]);

  const sendInstruction = () => {
    const text = input.trim();
    if (!text || !wsRef.current) return;
    addMessage(activeId, "user", text);
    setInput("");
    wsRef.current.send(JSON.stringify({ type: "instruction", project_id: activeId, content: text }));
    setAgentMap(prev => ({ ...prev, [activeId]: INITIAL_AGENTS() }));
  };

  const approve = (req: ApprovalRequest) => {
    wsRef.current?.send(JSON.stringify({ type: "approve", project_id: req.project_id, stage: req.stage }));
    setApprovals(prev => prev.filter(a => a.stage !== req.stage));
    addMessage(req.project_id, "user", `✓ '${req.stage}' 스테이지 승인`);
  };

  // 예전엔 카드만 화면에서 지우고 서버 상태(stages[stage].status === "waiting_approval")는
  // 그대로 둬서, 새로고침/WS 재연결 시 reconcileApprovals가 카드를 다시 복원시켰다
  // (거절해도 리셋 안 되는 버그). implement 승인 거절 = 디자인 목업이 마음에 안 든다는
  // 뜻이므로 기존 retry-design 엔드포인트로 실제 디자인 재작업을 트리거해 서버 상태를
  // PENDING으로 되돌린다. release 등 다른 승인 단계는 아직 거절 시 취할 동작이
  // 정의돼 있지 않아 범위 밖으로 남겨둔다.
  const reject = async (req: ApprovalRequest) => {
    if (req.stage !== "implement") {
      setApprovals(prev => prev.filter(a => a.stage !== req.stage));
      return;
    }
    const feedback = window.prompt("디자인을 어떻게 다시 만들지 알려주세요 (거절 사유):", "");
    if (feedback === null) return;
    setApprovals(prev => prev.filter(a => a.stage !== req.stage));
    addMessage(req.project_id, "user", `✗ '${req.stage}' 스테이지 거절${feedback ? `: ${feedback}` : ""}`);
    await fetch(`${API_URL}/projects/${req.project_id}/retry-design`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ feedback: feedback || "디자인 목업이 마음에 들지 않습니다. 다시 작업해주세요." }),
    }).catch(() => {});
  };

  // 승인 대기 중인 스테이지가 무엇이든, 요구사항 자체가 바뀌었으면 PM 기획부터
  // 전체 파이프라인을 다시 돌려야 한다(design 재작업만으로는 부족 — 스토리 구성
  // 자체가 바뀔 수 있음). RequirementModal에서 "전체 요구사항 교체"와 "기존
  // 범위 안에서 이번 요청/이슈 하나만 추가" 두 모드를 구분해 넘겨준다.
  // (예전엔 window.prompt를 썼는데, 브라우저 네이티브 prompt는 한 줄짜리
  // 고정폭 입력창이라 긴 요구사항을 쓰기엔 사실상 크기 제한이 있었다.)
  const retryPlanning = async (req: ApprovalRequest, fullRewrite: boolean, feedback: string, jiraIssue: string | null) => {
    setApprovals(prev => prev.filter(a => a.stage !== req.stage));
    addMessage(
      req.project_id, "user",
      `↺ 요구사항 갱신 — PM부터 재시작 (${fullRewrite ? "전체 교체" : "이번 요청"}${jiraIssue ? ` · ${jiraIssue}` : ""}): ${feedback}`
    );
    await fetch(`${API_URL}/projects/${req.project_id}/retry-planning`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ feedback, full_rewrite: fullRewrite, jira_issue: jiraIssue }),
    }).catch(() => {});
  };

  const createProject = async (name: string, repo: string) => {
    const res = await fetch(`${API_URL}/projects`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, github_repo: repo || null }),
    });
    if (res.ok) {
      const data = await res.json();
      if (data.github_url) {
        addMessage(data.project_id, "user", `✓ GitHub 레포: ${data.github_url}`);
      }
    }
  };

  const deactivateProject = async (projectId: string) => {
    await fetch(`${API_URL}/projects/${projectId}/deactivate`, { method: "POST" }).catch(() => {});
  };

  const activateProject = async (projectId: string) => {
    await fetch(`${API_URL}/projects/${projectId}/activate`, { method: "POST" }).catch(() => {});
  };

  // project_removed 브로드캐스트가 오면 프론트 상태(projects/activeId)는 위 WS
  // 핸들러가 알아서 정리한다 — 여기서는 확인창 + 삭제 요청만 담당.
  const discardProject = async (projectId: string, name: string) => {
    if (!window.confirm(`'${name}' 프로젝트를 폐기할까요? 되돌릴 수 없습니다.`)) return;
    const res = await fetch(`${API_URL}/projects/${projectId}`, { method: "DELETE" }).catch(() => null);
    if (res && !res.ok) {
      const data = await res.json().catch(() => null);
      window.alert(data?.detail || "프로젝트를 폐기하지 못했습니다.");
    }
  };

  // ── 탭 스위처 + 플로우차트 (공통, desktop/mobile 둘 다 씀) ────────
  const tabBar = (
    <div style={{ display: "flex", gap: 4, padding: "8px 20px 0", borderBottom: "0.5px solid #e5e5e5", flexShrink: 0 }}>
      {(["flow", "chat"] as const).map(tab => (
        <button key={tab} onClick={() => setActiveTab(tab)}
          style={{
            padding: "8px 14px", fontSize: 13, cursor: "pointer", border: "none", background: "transparent",
            borderBottom: activeTab === tab ? "2px solid #7F77DD" : "2px solid transparent",
            color: activeTab === tab ? "#4A3F9E" : "#888", fontWeight: activeTab === tab ? 500 : 400,
          }}>
          {tab === "flow" ? "🔀 스프린트 플로우" : "💬 대화"}
        </button>
      ))}
    </div>
  );

  const flowchartContent = (
    <FlowchartTab project={activeProject} projectId={activeId} onOpenDesign={() => setDesignOpen(true)} />
  );

  // ── 채팅 영역 (공통) ──────────────────────────────────────────
  const curApprovals = approvals.filter(a => a.project_id === activeId);

  const chatContent = (
    <>
      <div style={{ flex: 1, overflowY: "auto", padding: "16px", display: "flex", flexDirection: "column", gap: 12 }}>
        {messages.length === 0 && (
          <div style={{ color: "#aaa", fontSize: 13, textAlign: "center", marginTop: 60 }}>
            지시사항을 입력해 개발을 시작하세요.<br />
            <span style={{ fontSize: 12 }}>예: "로그인 화면 만들어줘"</span>
          </div>
        )}
        {messages.map(m => {
          const meta = AGENT_META[m.from] ?? { label: m.from, color: "#666", bg: "#f0f0f0" };
          const isUser = m.from === "user";
          return (
            <div key={m.id} style={{ display: "flex", gap: 8, flexDirection: isUser ? "row-reverse" : "row", alignItems: "flex-start" }}>
              <div style={{ width: 28, height: 28, borderRadius: "50%", background: meta.bg, color: meta.color, display: "flex", alignItems: "center", justifyContent: "center", fontSize: 11, fontWeight: 500, flexShrink: 0 }}>
                {meta.label.slice(0, 2)}
              </div>
              <div style={{ maxWidth: "75%" }}>
                <div style={{ fontSize: 11, color: "#aaa", marginBottom: 3, textAlign: isUser ? "right" : "left" }}>
                  {meta.label}{formatMessageTime(m.ts) && ` · ${formatMessageTime(m.ts)}`}
                </div>
                <div style={{ fontSize: 13, lineHeight: 1.6, padding: "8px 12px", background: isUser ? "#fff" : "#f5f4f0", border: "0.5px solid #e5e5e5", borderRadius: 8, whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
                  {linkifyContent(m.content)}
                </div>
                {m.video && (
                  <video key={m.video} controls preload="metadata" style={{ width: "100%", maxWidth: 360, marginTop: 6, borderRadius: 8, background: "#000" }}>
                    <source src={m.video} type="video/mp4" />
                  </video>
                )}
              </div>
            </div>
          );
        })}
        {curApprovals.map(req => (
          <div key={req.stage} style={{ background: "#fff", border: "0.5px solid #9FE1CB", borderRadius: 8, padding: "12px 14px" }}>
            <div style={{ fontSize: 12, fontWeight: 500, color: "#0F6E56", marginBottom: 4 }}>✓ 승인 필요 — {req.stage}</div>
            <div style={{ fontSize: 12, color: "#666", marginBottom: 8 }}>{req.message}</div>
            <div style={{ display: "flex", gap: 6 }}>
              <button onClick={() => approve(req)} style={{ flex: 1, padding: "6px", fontSize: 12, borderRadius: 6, background: "#EAF3DE", border: "0.5px solid #C0DD97", color: "#3B6D11", cursor: "pointer" }}>승인</button>
              <button onClick={() => reject(req)} style={{ flex: 1, padding: "6px", fontSize: 12, borderRadius: 6, background: "#f5f4f0", border: "0.5px solid #e5e5e5", color: "#666", cursor: "pointer" }}>거절</button>
              <button onClick={() => setRequirementReq(req)} style={{ flex: 1, padding: "6px", fontSize: 12, borderRadius: 6, background: "#F5F0FA", border: "0.5px solid #D9C7EC", color: "#6B3FA0", cursor: "pointer" }}>요구사항 변경</button>
            </div>
          </div>
        ))}
        <div ref={bottomRef} />
      </div>
      <div style={{ padding: "10px 12px", borderTop: "0.5px solid #e5e5e5", display: "flex", gap: 8 }}>
        <input value={input} onChange={e => setInput(e.target.value)}
          onKeyDown={e => e.key === "Enter" && !e.shiftKey && sendInstruction()}
          placeholder="지시사항 입력..."
          style={{ flex: 1, padding: "10px 12px", border: "0.5px solid #d5d5d5", borderRadius: 8, fontSize: 13, outline: "none" }} />
        <button onClick={sendInstruction} style={{ padding: "10px 16px", borderRadius: 8, background: "#7F77DD", border: "none", color: "#fff", fontSize: 13, cursor: "pointer" }}>전송</button>
      </div>
    </>
  );

  return (
    <>
      <style>{`
        * { box-sizing: border-box; }
        /* 모바일 브라우저의 100vh는 주소창이 접혔을 때(실제 보이는 뷰포트보다 큼)
           기준이라, 콘텐츠가 넘쳐서 안쪽 채팅 리스트가 아니라 body 전체가
           스크롤되며 헤더/입력창이 화면 밖으로 밀려나는 문제가 있었다. dvh는
           실제 보이는 뷰포트를 기준으로 하므로 헤더+입력창이 항상 화면 안에
           고정된다 — dvh 미지원 브라우저는 뒤 선언을 무시하고 앞의 vh로 폴백. */
        html, body { height: 100%; overflow: hidden; margin: 0; padding: 0; }
        .app-shell { height: 100vh; height: 100dvh; }
        @media (max-width: 768px) {
          .desktop-only { display: none !important; }
          .mobile-only  { display: flex !important; }
        }
        @media (min-width: 769px) {
          .mobile-only  { display: none !important; }
          .desktop-only { display: flex !important; }
        }
        .drawer-overlay { position:fixed;inset:0;z-index:50;background:rgba(0,0,0,0.3); }
        .drawer { position:fixed;top:0;left:0;bottom:0;z-index:51;width:240px;background:#f5f4f0;border-right:0.5px solid #e5e5e5;animation:slideIn .2s ease; }
        @keyframes slideIn { from{transform:translateX(-100%)} to{transform:translateX(0)} }
      `}</style>

      {showModal && <NewProjectModal onClose={() => setShowModal(false)} onCreate={(name, repo) => createProject(name, repo)} />}
      {requirementReq && (
        <RequirementModal
          onClose={() => setRequirementReq(null)}
          onSubmit={(fullRewrite, feedback, jiraIssue) => retryPlanning(requirementReq, fullRewrite, feedback, jiraIssue)}
        />
      )}
      <DetailPanel open={detailOpen} onClose={() => setDetailOpen(false)} agents={agents} projectId={activeId} />
      <OutputsPanel open={outputsOpen} onClose={() => setOutputsOpen(false)} projectId={activeId} />
      <DesignPanel open={designOpen} onClose={() => setDesignOpen(false)} projectId={activeId} />

      <div className="app-shell" style={{ display: "flex", flexDirection: "column", fontFamily: "system-ui, sans-serif", fontSize: 14, background: "#fafaf9", color: "#1a1a1a" }}>
        <TunnelBanner />

        {/* ── 데스크탑 (769px+) ─────────────────────────────────── */}
        <div className="desktop-only" style={{ flex: 1, minHeight: 0 }}>
          {/* 사이드바 (콜랩서블, 왼쪽으로 접힘) */}
          <div style={{ display: "flex", flexShrink: 0 }}>
            <div style={{
              width: sidebarCollapsed ? 0 : 200, overflow: "hidden", flexShrink: 0,
              background: "#f5f4f0", transition: "width 0.18s ease",
              borderRight: sidebarCollapsed ? "none" : "0.5px solid #e5e5e5",
            }}>
              <div style={{ width: 200, height: "100%" }}>
                <ProjectList projects={sortedProjects} activeId={activeId} onSelect={setActiveId} onCreate={() => setShowModal(true)} connected={connected}
                  githubRepos={githubRepos} repoLoading={repoLoading} onConnectRepo={r => createProject(r.name, r.full_name)} onRefreshRepos={fetchGithubRepos}
                  onDeactivate={deactivateProject} onActivate={activateProject} onDiscard={discardProject} />
              </div>
            </div>
            <button onClick={toggleSidebar} title={sidebarCollapsed ? "프로젝트 목록 펼치기" : "프로젝트 목록 접기"}
              style={{
                width: 16, flexShrink: 0, border: "none", borderRight: "0.5px solid #e5e5e5",
                background: "#f5f4f0", cursor: "pointer", color: "#bbb", fontSize: 10, padding: 0,
              }}>
              {sidebarCollapsed ? "›" : "‹"}
            </button>
          </div>

          {/* 채팅 (PM과의 대화) */}
          <div style={{ flex: 1, display: "flex", flexDirection: "column", minWidth: 0 }}>
            <div style={{ padding: "12px 20px", borderBottom: "0.5px solid #e5e5e5", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <div>
                <div style={{ fontWeight: 500 }}>{activeProject?.name ?? "AI 개발팀"}</div>
                <div style={{ fontSize: 12, color: "#888" }}>에이전트 {runningCount}개 실행 중</div>
              </div>
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <button onClick={() => setDesignOpen(true)} title="디자인 (적용된 디자인 · 적용 전 새 디자인, 시나리오별)"
                  style={{ fontSize: 16, color: "#888", border: "0.5px solid #e5e5e5", background: "#fff", padding: "4px 10px", borderRadius: 6, cursor: "pointer", lineHeight: 1 }}>🎨</button>
                <ProjectLinksMenu project={activeProject} />
                <button onClick={() => setOutputsOpen(true)} title="산출물 (디자인 · 영상 · 스크린샷, 최신순)"
                  style={{ fontSize: 16, color: "#888", border: "0.5px solid #e5e5e5", background: "#fff", padding: "4px 10px", borderRadius: 6, cursor: "pointer", lineHeight: 1 }}>🗂</button>
                <button onClick={() => setDetailOpen(true)} title="상세 정보 (팀 현황 · 로그 · PR)"
                  style={{ fontSize: 16, color: "#888", border: "0.5px solid #e5e5e5", background: "#fff", padding: "4px 10px", borderRadius: 6, cursor: "pointer", lineHeight: 1 }}>⋯</button>
                <a href="/docs" target="_blank" rel="noreferrer" style={headerLinkStyle}>API 문서</a>
              </div>
            </div>
            {tabBar}
            {activeTab === "flow" ? flowchartContent : chatContent}
          </div>
        </div>

        {/* ── 모바일 (≤768px) ──────────────────────────────────── */}
        <div className="mobile-only" style={{ flex: 1, flexDirection: "column", minHeight: 0 }}>

          {/* 드로어 */}
          {drawerOpen && (
            <>
              <div className="drawer-overlay" onClick={() => setDrawerOpen(false)} />
              <div className="drawer">
                <div style={{ padding: "14px 16px", borderBottom: "0.5px solid #e5e5e5", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                  <span style={{ fontSize: 13, fontWeight: 500, color: "#888" }}>프로젝트</span>
                  <button onClick={() => setDrawerOpen(false)} style={{ background: "none", border: "none", fontSize: 20, cursor: "pointer", color: "#888" }}>✕</button>
                </div>
                <ProjectList
                  projects={sortedProjects}
                  activeId={activeId}
                  onSelect={id => { setActiveId(id); setDrawerOpen(false); }}
                  onCreate={() => { setShowModal(true); setDrawerOpen(false); }}
                  connected={connected}
                  githubRepos={githubRepos}
                  repoLoading={repoLoading}
                  onConnectRepo={r => { createProject(r.name, r.full_name); setDrawerOpen(false); }}
                  onRefreshRepos={fetchGithubRepos}
                  onDeactivate={deactivateProject}
                  onActivate={activateProject}
                  onDiscard={discardProject}
                />
              </div>
            </>
          )}

          {/* 모바일 헤더 */}
          <div style={{ padding: "0 12px", height: 52, borderBottom: "0.5px solid #e5e5e5", display: "flex", alignItems: "center", gap: 10, background: "#fff", flexShrink: 0 }}>
            <button onClick={() => setDrawerOpen(true)} style={{ background: "none", border: "none", fontSize: 20, cursor: "pointer", color: "#555", padding: 4, lineHeight: 1 }}>☰</button>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontWeight: 500, fontSize: 15, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                {activeProject?.name ?? "AI 개발팀"}
              </div>
              {runningCount > 0 && <div style={{ fontSize: 11, color: "#EF9F27" }}>에이전트 {runningCount}개 실행 중</div>}
            </div>
            <button onClick={() => setDesignOpen(true)} title="디자인"
              style={{ background: "none", border: "none", fontSize: 18, cursor: "pointer", color: "#555", padding: 4, lineHeight: 1 }}>🎨</button>
            <ProjectLinksMenu project={activeProject} variant="mobile" />
            <button onClick={() => setOutputsOpen(true)} title="산출물"
              style={{ background: "none", border: "none", fontSize: 18, cursor: "pointer", color: "#555", padding: 4, lineHeight: 1 }}>🗂</button>
            <button onClick={() => setDetailOpen(true)} title="상세 정보"
              style={{ background: "none", border: "none", fontSize: 20, cursor: "pointer", color: "#555", padding: 4, lineHeight: 1 }}>⋯</button>
            <div style={{ width: 8, height: 8, borderRadius: "50%", background: connected ? "#1D9E75" : "#E24B4A", flexShrink: 0 }} />
          </div>

          {tabBar}
          <div style={{ flex: 1, display: "flex", flexDirection: "column", minHeight: 0 }}>
            {activeTab === "flow" ? flowchartContent : chatContent}
          </div>
        </div>
      </div>
    </>
  );
}
