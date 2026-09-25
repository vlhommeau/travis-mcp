// Shapes Travis API v3 payloads into compact objects for the model, and trims logs.

// eslint-disable-next-line no-control-regex
const ANSI_PATTERN = /\u001b\[[0-9;?]*[A-Za-z]|\u001b\][^\u0007]*\u0007/g;
// Timing and folding markers Travis interleaves in raw logs; meaningless outside the web UI.
const TRAVIS_MARKER_PATTERN = /^travis_(time|fold):/;

export function summarizeBuild(build, client) {
  const slug = build.repository?.slug;
  return {
    id: build.id,
    number: build.number,
    state: build.state,
    previous_state: build.previous_state,
    event_type: build.event_type,
    branch: build.branch?.name,
    pull_request_number: build.pull_request_number ?? undefined,
    pull_request_title: build.pull_request_title ?? undefined,
    commit: build.commit
      ? {
          sha: build.commit.sha,
          message: firstLine(build.commit.message),
        }
      : undefined,
    started_at: build.started_at,
    finished_at: build.finished_at,
    duration_seconds: build.duration,
    jobs: build.jobs?.map((job) => job.id),
    url: slug ? client.buildWebUrl(slug, build.id) : undefined,
  };
}

export function summarizeJob(job, repoSlug, client) {
  return {
    id: job.id,
    number: job.number,
    state: job.state,
    stage: job.stage?.name,
    allow_failure: job.allow_failure,
    started_at: job.started_at,
    finished_at: job.finished_at,
    url: repoSlug ? client.jobWebUrl(repoSlug, job.id) : undefined,
  };
}

export function trimLog(rawLog, { tailLines, grep, contextLines = 3 }) {
  const lines = rawLog
    .replace(ANSI_PATTERN, '')
    .replace(/\r(?!\n)/g, '\n')
    .split('\n')
    .filter((line) => !TRAVIS_MARKER_PATTERN.test(line));
  const totalLines = lines.length;

  if (grep) {
    const pattern = new RegExp(grep, 'i');
    const keep = new Set();
    lines.forEach((line, index) => {
      if (pattern.test(line)) {
        for (let i = Math.max(0, index - contextLines); i <= Math.min(totalLines - 1, index + contextLines); i++) {
          keep.add(i);
        }
      }
    });
    const selected = [...keep].sort((a, b) => a - b);
    const output = [];
    let previous = -2;
    for (const index of selected) {
      if (index !== previous + 1) {
        output.push(`--- line ${index + 1} ---`);
      }
      output.push(lines[index]);
      previous = index;
    }
    return { totalLines, text: output.join('\n'), truncated: selected.length < totalLines };
  }

  if (tailLines && totalLines > tailLines) {
    return {
      totalLines,
      text: lines.slice(-tailLines).join('\n'),
      truncated: true,
    };
  }

  return { totalLines, text: lines.join('\n'), truncated: false };
}

function firstLine(text) {
  return typeof text === 'string' ? text.split('\n')[0] : text;
}
