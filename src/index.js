#!/usr/bin/env node
// MCP server for Travis CI. Read-only by default: build, job and log lookups.
// Build restart/cancel tools are registered only when opted in via TRAVIS_ALLOW_WRITE;
// nothing ever triggers a new build with custom config, or reads env vars / settings.

import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { z } from 'zod';
import { TravisClient } from './travis.js';
import { summarizeBuild, summarizeJob, trimLog } from './format.js';
import { parseAllowedWrites } from './config.js';

const client = new TravisClient({
  token: process.env.TRAVIS_API_TOKEN,
  apiUrl: process.env.TRAVIS_API_URL,
  webUrl: process.env.TRAVIS_WEB_URL,
});
const defaultRepo = process.env.TRAVIS_DEFAULT_REPO || undefined;
const allowedWrites = parseAllowedWrites(process.env.TRAVIS_ALLOW_WRITE);

const server = new McpServer({ name: 'travis-mcp', version: '0.2.0' });

const readOnly = { readOnlyHint: true, destructiveHint: false, openWorldHint: true };
// Restart replaces the previous run's log and result; cancel stops work in progress.
const write = { readOnlyHint: false, destructiveHint: true, idempotentHint: false, openWorldHint: true };

const buildId = z
  .union([z.number().int().positive(), z.string().regex(/^\d+$/)])
  .describe('Travis build ID, the number in .../builds/<id> URLs');

server.registerTool(
  'get_build',
  {
    title: 'Get build',
    description: 'State, branch/PR, commit, timing and job IDs of one Travis build.',
    inputSchema: { build_id: buildId },
    annotations: readOnly,
  },
  async ({ build_id }) => json(summarizeBuild(await client.getBuild(build_id), client)),
);

server.registerTool(
  'get_build_jobs',
  {
    title: 'Get build jobs',
    description: 'Jobs of one Travis build with their state and stage, to find which job failed.',
    inputSchema: { build_id: buildId },
    annotations: readOnly,
  },
  async ({ build_id }) => {
    const build = await client.getBuild(build_id);
    const { jobs } = await client.getBuildJobs(build_id);
    const slug = build.repository?.slug;
    return json({
      build: { id: build.id, number: build.number, state: build.state },
      jobs: jobs.map((job) => summarizeJob(job, slug, client)),
    });
  },
);

server.registerTool(
  'list_builds',
  {
    title: 'List builds',
    description:
      'Most recent builds of a repository, newest first. Filter by branch, event type, state or pull request number.',
    inputSchema: {
      repo: z
        .string()
        .regex(/^[\w.-]+\/[\w.-]+$/)
        .optional()
        .describe(`Repository slug "owner/name"${defaultRepo ? `, defaults to ${defaultRepo}` : ''}`),
      branch: z.string().optional().describe('Branch name (for PR builds: the target branch)'),
      pull_request: z
        .number()
        .int()
        .positive()
        .optional()
        .describe('Only builds of this pull request number (implies event_type=pull_request)'),
      event_type: z.enum(['push', 'pull_request', 'api', 'cron']).optional(),
      state: z.enum(['created', 'received', 'started', 'passed', 'failed', 'errored', 'canceled']).optional(),
      limit: z.number().int().min(1).max(50).default(10),
    },
    annotations: readOnly,
  },
  async ({ repo, branch, pull_request, event_type, state, limit }) => {
    const slug = repo ?? defaultRepo;
    if (!slug) {
      throw new Error('repo is required (no TRAVIS_DEFAULT_REPO configured)');
    }

    if (pull_request === undefined) {
      const { builds } = await client.listBuilds(slug, { branch, eventType: event_type, state, limit });
      return json(builds.map((build) => summarizeBuild({ repository: { slug }, ...build }, client)));
    }

    // The API has no pull request filter: page through PR builds and filter client side.
    const matches = [];
    for (let offset = 0; offset < 500 && matches.length < limit; offset += 100) {
      const { builds } = await client.listBuilds(slug, {
        branch,
        eventType: 'pull_request',
        state,
        limit: 100,
        offset,
      });
      matches.push(...builds.filter((build) => build.pull_request_number === pull_request));
      if (builds.length < 100) {
        break;
      }
    }
    return json(matches.slice(0, limit).map((build) => summarizeBuild({ repository: { slug }, ...build }, client)));
  },
);

server.registerTool(
  'get_job_log',
  {
    title: 'Get job log',
    description:
      'Plain-text log of one Travis job, ANSI codes stripped. Returns the last lines by default; use grep to extract matching lines with context instead.',
    inputSchema: {
      job_id: z
        .union([z.number().int().positive(), z.string().regex(/^\d+$/)])
        .describe('Travis job ID, from get_build_jobs'),
      tail_lines: z.number().int().min(1).max(5000).default(200).describe('Number of trailing lines to return'),
      grep: z.string().optional().describe('Case-insensitive regex; returns matching lines with 3 lines of context'),
    },
    annotations: readOnly,
  },
  async ({ job_id, tail_lines, grep }) => {
    const raw = await client.getJobLog(job_id);
    // Travis answers a literal "null" body when the job has no stored log (not archived yet, or removed).
    if (raw.trim() === '' || raw.trim() === 'null') {
      return { content: [{ type: 'text', text: `job ${job_id}: no log available from the Travis API` }] };
    }
    const log = trimLog(raw, { tailLines: tail_lines, grep });
    const header = `job ${job_id}: ${log.totalLines} lines${log.truncated ? ' (trimmed)' : ''}`;
    return { content: [{ type: 'text', text: `${header}\n\n${log.text}` }] };
  },
);

const writeTools = {
  restart: {
    name: 'restart_build',
    title: 'Restart build',
    description:
      'Restart every job of a finished Travis build. Replaces the previous run\'s logs and result, and consumes concurrency slots. Only call on an explicit human instruction naming this build.',
    run: (id) => client.restartBuild(id),
  },
  cancel: {
    name: 'cancel_build',
    title: 'Cancel build',
    description:
      'Cancel a created or running Travis build (all its jobs). Only call on an explicit human instruction naming this build.',
    run: (id) => client.cancelBuild(id),
  },
};

for (const action of allowedWrites) {
  const tool = writeTools[action];
  server.registerTool(
    tool.name,
    {
      title: tool.title,
      description: tool.description,
      inputSchema: { build_id: buildId },
      annotations: write,
    },
    async ({ build_id }) => {
      const before = summarizeBuild(await client.getBuild(build_id), client);
      const result = await tool.run(build_id);
      return json({ action, accepted: result['@type'] === 'pending', state_before: before.state, build: before });
    },
  );
}

function json(value) {
  return { content: [{ type: 'text', text: JSON.stringify(value, null, 2) }] };
}

await server.connect(new StdioServerTransport());
