const TIMESTAMP_LINE =
  /^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})\s+([A-Z]+)\s+(\S+)\s+([\s\S]*)$/;
const REQUEST_ID_CAPTURE = /request_id=([0-9a-fA-F-]{36})/;
const MAX_PARSED_LINES = 4000;
const MAX_LOGS_PER_BUCKET = 10;
const MAX_REQUEST_LOGS = 16;
const MAX_ORCHESTRATOR_LOGS = 8;
const MAX_RECENT_REQUESTS = 8;

function parseTimestamp(rawTimestamp) {
  const [datePart, timePart] = rawTimestamp.split(" ");
  const normalized = `${datePart}T${timePart.replace(",", ".")}`;
  const parsed = new Date(normalized);
  if (Number.isNaN(parsed.getTime())) {
    return null;
  }
  return parsed.toISOString();
}

function clipText(value, maxLen = 220) {
  const normalized = String(value ?? "").replace(/\s+/g, " ").trim();
  if (!normalized) {
    return "";
  }
  if (normalized.length <= maxLen) {
    return normalized;
  }
  return `${normalized.slice(0, maxLen - 3)}...`;
}

function parseMessageRequestId(message) {
  const match = message.match(REQUEST_ID_CAPTURE);
  return match ? match[1] : null;
}

function pushBounded(items, value, maxCount) {
  items.push(value);
  if (items.length > maxCount) {
    items.splice(0, items.length - maxCount);
  }
}

function ensureRequest(requests, requestId) {
  if (!requests.has(requestId)) {
    requests.set(requestId, {
      id: requestId,
      taskDescription: "",
      status: "running",
      branch: "unknown",
      acceptedAt: null,
      completedAt: null,
      lastUpdatedAt: null,
      logs: [],
      subagentOrder: [],
      subagents: new Map(),
    });
  }
  return requests.get(requestId);
}

function ensureSubagent(request, subagentName) {
  if (!request.subagents.has(subagentName)) {
    request.subagents.set(subagentName, {
      name: subagentName,
      status: "idle",
      description: "",
      lastUpdatedAt: null,
      logs: [],
    });
    request.subagentOrder.push(subagentName);
  }
  return request.subagents.get(subagentName);
}

function addRequestLog(request, timestamp, text) {
  if (!text) {
    return;
  }
  pushBounded(
    request.logs,
    {
      timestamp,
      text: clipText(text),
    },
    MAX_REQUEST_LOGS
  );
}

function addSubagentLog(subagent, timestamp, text) {
  if (!text) {
    return;
  }
  pushBounded(
    subagent.logs,
    {
      timestamp,
      text: clipText(text),
    },
    MAX_LOGS_PER_BUCKET
  );
}

function markRequestCompletion(request, status, timestamp) {
  request.status = status;
  request.completedAt = timestamp;
  request.lastUpdatedAt = timestamp;
  for (const subagent of request.subagents.values()) {
    if (subagent.status === "running" || subagent.status === "idle") {
      subagent.status = status;
      subagent.lastUpdatedAt = timestamp;
    }
  }
}

function parseStructuredLine(line) {
  const match = line.match(TIMESTAMP_LINE);
  if (!match) {
    return null;
  }
  return {
    rawTimestamp: match[1],
    timestamp: parseTimestamp(match[1]),
    level: match[2],
    logger: match[3],
    message: match[4],
  };
}

function buildLogicalLines(logContent) {
  const physicalLines = logContent
    .split(/\r?\n/)
    .slice(-MAX_PARSED_LINES)
    .filter((line) => line.length > 0);

  const logicalLines = [];
  let current = "";

  for (const line of physicalLines) {
    if (TIMESTAMP_LINE.test(line)) {
      if (current) {
        logicalLines.push(current);
      }
      current = line;
      continue;
    }

    if (current) {
      current = `${current}\n${line}`;
    }
  }

  if (current) {
    logicalLines.push(current);
  }

  return logicalLines;
}

function emptyState(logPath, warnings = []) {
  return {
    generatedAt: new Date().toISOString(),
    logPath,
    warnings,
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
}

function deriveSubagentState(activeRequest, orderedRequests, knownSubagentNames) {
  const latestByName = new Map();
  for (const request of orderedRequests) {
    for (const [subagentName, subagent] of request.subagents.entries()) {
      latestByName.set(subagentName, {
        request,
        subagent,
      });
      knownSubagentNames.add(subagentName);
    }
  }

  const cards = [];
  const sortedNames = Array.from(knownSubagentNames).sort((left, right) =>
    left.localeCompare(right)
  );

  for (const subagentName of sortedNames) {
    const activeMatch = activeRequest
      ? activeRequest.subagents.get(subagentName)
      : null;
    const fallback = latestByName.get(subagentName);
    const sourceSubagent = activeMatch ?? fallback?.subagent ?? null;
    const sourceRequest = activeMatch ? activeRequest : fallback?.request ?? null;

    let status = "idle";
    if (activeMatch) {
      if (activeRequest.status === "running") {
        status = "running";
      } else {
        status = activeRequest.status;
      }
    }

    cards.push({
      name: subagentName,
      status,
      requestId: sourceRequest?.id ?? null,
      description: sourceSubagent?.description ?? "",
      lastUpdatedAt: sourceSubagent?.lastUpdatedAt ?? sourceRequest?.lastUpdatedAt ?? null,
      logs: sourceSubagent ? sourceSubagent.logs.slice(-MAX_LOGS_PER_BUCKET) : [],
    });
  }

  return cards;
}

export function buildDemoStateFromLog(logContent, logPath) {
  if (!logContent || !logContent.trim()) {
    return emptyState(logPath);
  }

  const warnings = [];
  const lines = buildLogicalLines(logContent);

  const requests = new Map();
  const knownSubagentNames = new Set();

  let latestRequestHint = null;

  for (const rawLine of lines) {
    const parsed = parseStructuredLine(rawLine);
    if (!parsed || !parsed.timestamp) {
      continue;
    }

    const { timestamp, message } = parsed;

    const acceptedMatch = message.match(
      /^accepted orchestrate request_id=([0-9a-fA-F-]{36}) task_description=([\s\S]*?)(?:\s+metadata=[\s\S]*)?$/
    );
    if (acceptedMatch) {
      const requestId = acceptedMatch[1];
      const request = ensureRequest(requests, requestId);
      request.status = "running";
      request.taskDescription = clipText(acceptedMatch[2], 260);
      request.acceptedAt = timestamp;
      request.lastUpdatedAt = timestamp;
      addRequestLog(request, timestamp, `Accepted task: ${request.taskDescription}`);
      latestRequestHint = requestId;
      continue;
    }

    const explicitRequestId = parseMessageRequestId(message);
    if (explicitRequestId) {
      latestRequestHint = explicitRequestId;
    }

    const routeMatch = message.match(/^orchestrator route=(match|build_new)\b(.*)$/);
    if (routeMatch) {
      const routeRequestId = explicitRequestId ?? latestRequestHint;
      const routeRequest = routeRequestId
        ? ensureRequest(requests, routeRequestId)
        : null;
      if (!routeRequest) {
        continue;
      }

      routeRequest.branch = routeMatch[1];
      routeRequest.lastUpdatedAt = timestamp;
      const subagentRef = routeMatch[2].match(
        /\bsubagent(?:_name)?=([a-zA-Z0-9_.:-]+)\b/
      );
      if (subagentRef) {
        addRequestLog(
          routeRequest,
          timestamp,
          `Route decided: ${routeMatch[1]} (${subagentRef[1]})`
        );
      } else {
        addRequestLog(routeRequest, timestamp, `Route decided: ${routeMatch[1]}`);
      }
      continue;
    }

    const buildNewRunMatch = message.match(
      /^orchestrator route=build_new running newly available subagent=([a-zA-Z0-9_.:-]+) request_id=([0-9a-fA-F-]{36})$/
    );
    if (buildNewRunMatch) {
      const subagentName = buildNewRunMatch[1];
      const explicitRequest = ensureRequest(requests, buildNewRunMatch[2]);
      const subagent = ensureSubagent(explicitRequest, subagentName);
      subagent.status = "running";
      subagent.lastUpdatedAt = timestamp;
      explicitRequest.branch = "build_new";
      explicitRequest.lastUpdatedAt = timestamp;
      addRequestLog(explicitRequest, timestamp, `Running subagent: ${subagentName}`);
      knownSubagentNames.add(subagentName);
      continue;
    }

    const runningSubagentMatch = message.match(
      /^running subagent request_id=([0-9a-fA-F-]{36}) subagent=([a-zA-Z0-9_.:-]+) description=(.*)$/
    );
    if (runningSubagentMatch) {
      const runningRequest = ensureRequest(requests, runningSubagentMatch[1]);
      const subagentName = runningSubagentMatch[2];
      const subagent = ensureSubagent(runningRequest, subagentName);
      subagent.status = "running";
      subagent.description = clipText(runningSubagentMatch[3], 220);
      subagent.lastUpdatedAt = timestamp;
      runningRequest.lastUpdatedAt = timestamp;
      addRequestLog(runningRequest, timestamp, `Running subagent: ${subagentName}`);
      knownSubagentNames.add(subagentName);
      latestRequestHint = runningRequest.id;
      continue;
    }

    const claudeStartMatch = message.match(
      /^starting claude run request_id=([0-9a-fA-F-]{36}) subagent=([a-zA-Z0-9_.:-]+).*$/
    );
    if (claudeStartMatch) {
      const claudeRequest = ensureRequest(requests, claudeStartMatch[1]);
      const subagent = ensureSubagent(claudeRequest, claudeStartMatch[2]);
      subagent.status = "running";
      subagent.lastUpdatedAt = timestamp;
      claudeRequest.lastUpdatedAt = timestamp;
      addSubagentLog(subagent, timestamp, "Claude run started");
      continue;
    }

    const eventTextMatch = message.match(
      /^claude event text request_id=([0-9a-fA-F-]{36}) subagent=([a-zA-Z0-9_.:-]+) idx=\d+ text=(.*)$/
    );
    if (eventTextMatch) {
      const eventRequest = ensureRequest(requests, eventTextMatch[1]);
      const subagent = ensureSubagent(eventRequest, eventTextMatch[2]);
      subagent.status = "running";
      subagent.lastUpdatedAt = timestamp;
      eventRequest.lastUpdatedAt = timestamp;
      addSubagentLog(subagent, timestamp, eventTextMatch[3]);
      continue;
    }

    const eventToolsMatch = message.match(
      /^claude event tools request_id=([0-9a-fA-F-]{36}) subagent=([a-zA-Z0-9_.:-]+) idx=\d+ tools=(.*)$/
    );
    if (eventToolsMatch) {
      const eventRequest = ensureRequest(requests, eventToolsMatch[1]);
      const subagent = ensureSubagent(eventRequest, eventToolsMatch[2]);
      subagent.status = "running";
      subagent.lastUpdatedAt = timestamp;
      eventRequest.lastUpdatedAt = timestamp;
      addSubagentLog(subagent, timestamp, `Using tools: ${eventToolsMatch[3]}`);
      continue;
    }

    const eventResultMatch = message.match(
      /^claude event result request_id=([0-9a-fA-F-]{36}) subagent=([a-zA-Z0-9_.:-]+) idx=\d+ text=(.*)$/
    );
    if (eventResultMatch) {
      const eventRequest = ensureRequest(requests, eventResultMatch[1]);
      const subagent = ensureSubagent(eventRequest, eventResultMatch[2]);
      subagent.status = "running";
      subagent.lastUpdatedAt = timestamp;
      eventRequest.lastUpdatedAt = timestamp;
      addSubagentLog(subagent, timestamp, `Result ready: ${eventResultMatch[3]}`);
      continue;
    }

    const callbackSentMatch = message.match(
      /^poke callback sent request_id=([0-9a-fA-F-]{36}) status=(\d+).*$/
    );
    if (callbackSentMatch) {
      const callbackRequest = ensureRequest(requests, callbackSentMatch[1]);
      const callbackStatus = callbackSentMatch[2].startsWith("2")
        ? "completed"
        : "failed";
      markRequestCompletion(callbackRequest, callbackStatus, timestamp);
      addRequestLog(
        callbackRequest,
        timestamp,
        callbackStatus === "completed"
          ? "Callback delivered to Poke"
          : "Callback delivery failed"
      );
      continue;
    }

    const requestErrorMatch = message.match(
      /^Error while processing request ([0-9a-fA-F-]{36}): (.*)$/
    );
    if (requestErrorMatch) {
      const failedRequest = ensureRequest(requests, requestErrorMatch[1]);
      markRequestCompletion(failedRequest, "failed", timestamp);
      addRequestLog(failedRequest, timestamp, requestErrorMatch[2]);
      continue;
    }

  }

  const orderedRequests = Array.from(requests.values()).sort((left, right) => {
    const leftTs = Date.parse(left.acceptedAt ?? left.lastUpdatedAt ?? "");
    const rightTs = Date.parse(right.acceptedAt ?? right.lastUpdatedAt ?? "");
    return leftTs - rightTs;
  });

  const runningRequests = orderedRequests.filter(
    (request) => request.status === "running"
  );
  const activeRequest =
    runningRequests[runningRequests.length - 1] ??
    orderedRequests[orderedRequests.length - 1] ??
    null;

  if (!activeRequest && orderedRequests.length === 0) {
    warnings.push(
      "No orchestrator activity found yet."
    );
  }

  const subagents = deriveSubagentState(
    activeRequest,
    orderedRequests,
    knownSubagentNames
  );

  const orchestrator = activeRequest
    ? {
        status:
          activeRequest.status === "running" ? "running" : activeRequest.status,
        requestId: activeRequest.id,
        taskDescription: activeRequest.taskDescription,
        branch: activeRequest.branch,
        startedAt: activeRequest.acceptedAt,
        lastUpdatedAt: activeRequest.lastUpdatedAt,
        logs: activeRequest.logs.slice(-MAX_ORCHESTRATOR_LOGS),
      }
    : {
        status: "idle",
        requestId: null,
        taskDescription: "",
        branch: "unknown",
        startedAt: null,
        lastUpdatedAt: null,
        logs: [],
      };

  const recentRequests = orderedRequests
    .slice(-MAX_RECENT_REQUESTS)
    .reverse()
    .map((request) => ({
      id: request.id,
      status: request.status,
      branch: request.branch,
      taskDescription: request.taskDescription,
      acceptedAt: request.acceptedAt,
      completedAt: request.completedAt,
      subagents: request.subagentOrder.slice(),
    }));

  return {
    generatedAt: new Date().toISOString(),
    logPath,
    warnings,
    orchestrator,
    subagents,
    recentRequests,
  };
}
