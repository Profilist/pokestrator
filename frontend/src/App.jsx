import { memo, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import {
  Background,
  Controls,
  Handle,
  Position,
  ReactFlow,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";

const POLL_INTERVAL_MS = 1200;

const EMPTY_STATE = {
  generatedAt: null,
  logPath: "",
  warnings: [],
  orchestrator: {
    status: "idle",
    requestId: null,
    taskDescription: "",
    branch: "unknown",
    startedAt: null,
    lastUpdatedAt: null,
    logs: [],
  },
  subagents: [],
  recentRequests: [],
};

const STATUS_LABELS = {
  idle: "Idle",
  running: "Running",
  completed: "Completed",
  failed: "Failed",
};

function normalizeStatus(status) {
  if (status === "running" || status === "completed" || status === "failed") {
    return status;
  }
  return "idle";
}

function formatClock(timestamp) {
  if (!timestamp) {
    return "--:--:--";
  }
  const parsed = new Date(timestamp);
  if (Number.isNaN(parsed.getTime())) {
    return "--:--:--";
  }
  return parsed.toLocaleTimeString([], { hour12: false });
}

function StatusTag({ status }) {
  const normalized = normalizeStatus(status);
  return (
    <span className={`status-tag status-${normalized}`}>
      {STATUS_LABELS[normalized]}
    </span>
  );
}

function LogWindow({ logs }) {
  const logRef = useRef(null);

  const lastLogKey =
    logs && logs.length > 0
      ? `${logs[logs.length - 1].timestamp ?? ""}-${logs[logs.length - 1].text ?? ""}`
      : "";

  useLayoutEffect(() => {
    const element = logRef.current;
    if (!element) {
      return;
    }
    element.scrollTop = element.scrollHeight;
  }, [lastLogKey, logs?.length]);

  function handleWheelCapture(event) {
    const element = logRef.current;
    if (!element) {
      return;
    }
    const maxScroll = element.scrollHeight - element.clientHeight;
    if (maxScroll <= 0) {
      return;
    }
    event.preventDefault();
    event.stopPropagation();
    const nextTop = element.scrollTop + event.deltaY;
    element.scrollTop = Math.max(0, Math.min(maxScroll, nextTop));
  }

  if (!logs || logs.length === 0) {
    return <div className="log-empty">Waiting for new events...</div>;
  }

  return (
    <div
      ref={logRef}
      className="log-window nowheel nodrag nopan"
      onWheelCapture={handleWheelCapture}
      onPointerDown={(event) => event.stopPropagation()}
      onMouseDown={(event) => event.stopPropagation()}
    >
      {logs.map((entry, index) => (
        <div
          className="log-line"
          key={`${entry.timestamp ?? "no-time"}-${index}-${entry.text ?? ""}`}
        >
          <span className="log-time">{formatClock(entry.timestamp)}</span>
          <span className="log-text">{entry.text}</span>
        </div>
      ))}
    </div>
  );
}

function AgentCard({
  name,
  subtitle,
  status,
  requestId,
  logs,
  className = "",
}) {
  const normalizedStatus = normalizeStatus(status);

  return (
    <article className={`agent-card status-${normalizedStatus} ${className}`.trim()}>
      <div className="card-topline">
        <h2>{name}</h2>
        <StatusTag status={normalizedStatus} />
      </div>
      <p className="card-subtitle">{subtitle}</p>
      <p className="card-meta">request: {requestId ?? "none"}</p>
      <LogWindow logs={logs} />
    </article>
  );
}

const HIDDEN_HANDLE_STYLE = {
  opacity: 0,
  width: 12,
  height: 12,
  background: "transparent",
  border: "none",
  pointerEvents: "none",
};

const AgentFlowNode = memo(function AgentFlowNode({ data }) {
  return (
    <div className="flow-node-shell">
      <Handle
        type="target"
        position={Position.Top}
        isConnectable={false}
        style={HIDDEN_HANDLE_STYLE}
      />
      <AgentCard
        name={data.name}
        subtitle={data.subtitle}
        status={data.status}
        requestId={data.requestId}
        logs={data.logs}
        className={data.className ?? ""}
      />
      <Handle
        type="source"
        position={Position.Bottom}
        isConnectable={false}
        style={HIDDEN_HANDLE_STYLE}
      />
    </div>
  );
});

const FLOW_NODE_TYPES = { agent: AgentFlowNode };

function subagentNodeId(name) {
  return `subagent-${String(name ?? "")
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, "_")}`;
}

export default function App() {
  const [state, setState] = useState(EMPTY_STATE);
  const [fetchError, setFetchError] = useState("");

  useEffect(() => {
    let isCancelled = false;

    async function refresh() {
      try {
        const response = await fetch("/api/demo-state", {
          cache: "no-store",
        });
        if (!response.ok) {
          throw new Error(`HTTP ${response.status}`);
        }
        const payload = await response.json();
        if (!isCancelled) {
          setState(payload);
          setFetchError("");
        }
      } catch (error) {
        if (!isCancelled) {
          setFetchError(
            `Dashboard API unavailable: ${String(error?.message ?? error)}`
          );
        }
      }
    }

    refresh();
    const timer = window.setInterval(refresh, POLL_INTERVAL_MS);
    return () => {
      isCancelled = true;
      window.clearInterval(timer);
    };
  }, []);

  const orchestrator = state.orchestrator ?? EMPTY_STATE.orchestrator;
  const subagents = state.subagents ?? [];
  const recentRequests = state.recentRequests ?? [];

  const warnings = useMemo(() => {
    const warningList = [];
    if (fetchError) {
      warningList.push(fetchError);
    }
    if (Array.isArray(state.warnings)) {
      warningList.push(...state.warnings);
    }
    return warningList;
  }, [fetchError, state.warnings]);

  const flowNodes = useMemo(() => {
    const orchestratorSubtitle = orchestrator.taskDescription
      ? `Task: ${orchestrator.taskDescription}`
      : "No active orchestration request";

    const nodes = [
      {
        id: "orchestrator",
        type: "agent",
        position: { x: 0, y: 20 },
        draggable: false,
        selectable: false,
        data: {
          name: "Orchestrator",
          subtitle: orchestratorSubtitle,
          status: orchestrator.status,
          requestId: orchestrator.requestId,
          logs: orchestrator.logs,
          className: "orchestrator-card",
        },
      },
    ];

    const cardSpacing = 390;
    const startX = -((subagents.length - 1) * cardSpacing) / 2;
    const rowY = 470;

    subagents.forEach((subagent, index) => {
      nodes.push({
        id: subagentNodeId(subagent.name),
        type: "agent",
        position: { x: startX + index * cardSpacing, y: rowY },
        draggable: false,
        selectable: false,
        data: {
          name: subagent.name,
          subtitle:
            subagent.description || "Waiting for run-specific details.",
          status: subagent.status,
          requestId: subagent.requestId,
          logs: subagent.logs,
        },
      });
    });

    return nodes;
  }, [orchestrator, subagents]);

  const flowEdges = useMemo(
    () =>
      subagents.map((subagent) => {
        const isRunning = normalizeStatus(subagent.status) === "running";
        return {
          id: `edge-orchestrator-${subagentNodeId(subagent.name)}`,
          source: "orchestrator",
          target: subagentNodeId(subagent.name),
          type: "smoothstep",
          animated: isRunning,
          className: isRunning ? "flow-edge-running" : "flow-edge-idle",
          style: {
            stroke: isRunning ? "#0b9e6f" : "#7daca0",
            strokeWidth: isRunning ? 2.5 : 2.1,
            strokeDasharray: isRunning ? "10 7" : "0",
            strokeLinecap: "round",
          },
        };
      }),
    [subagents]
  );

  return (
    <div className="app-shell">
      <header className="topbar">
        <div>
          <div className="brandline">
            <img
              className="brand-logo"
              src="/logo.png"
              alt="Pokestrator logo"
              onError={(event) => {
                event.currentTarget.style.display = "none";
              }}
            />
            <p className="eyebrow">Pokestrator</p>
          </div>
        </div>
      </header>

      {warnings.length > 0 ? (
        <section className="warning-panel">
          {warnings.map((warning, index) => (
            <p key={`${warning}-${index}`}>{warning}</p>
          ))}
        </section>
      ) : null}

      <main className="diagram-layout">
        <section className="flow-section">
          <div className="flow-canvas">
            <ReactFlow
              nodes={flowNodes}
              edges={flowEdges}
              nodeTypes={FLOW_NODE_TYPES}
              fitView
              fitViewOptions={{ padding: 0.2, duration: 280 }}
              nodesDraggable
              nodesConnectable={false}
              elementsSelectable
              panOnDrag
              zoomOnDoubleClick
              zoomOnPinch
              zoomOnScroll
              preventScrolling={false}
              proOptions={{ hideAttribution: true }}
            >
              <Controls />
              <Background color="rgba(75, 101, 93, 0.14)" gap={20} />
            </ReactFlow>
          </div>
          <p className="branch-note">
            route branch: {orchestrator.branch || "unknown"}
          </p>
          {subagents.length === 0 ? (
            <p className="empty-note flow-empty-note">
              No subagent activity yet. Trigger a new `orchestrate(...)` call to
              populate this graph.
            </p>
          ) : null}
        </section>
      </main>

      <section className="history-section">
        <h2>Recent Requests</h2>
        {recentRequests.length === 0 ? (
          <p className="empty-note">No requests observed in current log window.</p>
        ) : (
          <div className="history-list">
            {recentRequests.map((request) => (
              <article className="history-item" key={request.id}>
                <div className="history-topline">
                  <code>{request.id}</code>
                  <StatusTag status={request.status} />
                </div>
                <p className="history-task">{request.taskDescription || "n/a"}</p>
                <p className="history-meta">
                  branch: {request.branch || "unknown"} | subagents:{" "}
                  {request.subagents.length > 0
                    ? request.subagents.join(", ")
                    : "none"}
                </p>
              </article>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
