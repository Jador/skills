import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  ListToolsRequestSchema,
  CallToolRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";
import { mkdirSync, readdirSync, unlinkSync, existsSync } from "node:fs";
import { execSync } from "node:child_process";
import { homedir } from "node:os";
import { join } from "node:path";

// ---------------------------------------------------------------------------
// Data directory
// ---------------------------------------------------------------------------

const dataDir =
  process.env.CLAUDE_PLUGIN_DATA ||
  join(homedir(), ".claude", "plugins", "data", "jador-skills");

function babysitDir(): string {
  return join(dataDir, "babysit");
}

function ensureBabysitDir(): void {
  mkdirSync(babysitDir(), { recursive: true });
}

// ---------------------------------------------------------------------------
// State helpers
// ---------------------------------------------------------------------------

export function readSeenComments(pr: string): number[] {
  const path = join(babysitDir(), `${pr}-seen-comments.json`);
  try {
    const text = require("fs").readFileSync(path, "utf-8");
    return JSON.parse(text) as number[];
  } catch {
    return [];
  }
}

export function writeSeenComments(pr: string, ids: number[]): void {
  ensureBabysitDir();
  const path = join(babysitDir(), `${pr}-seen-comments.json`);
  Bun.write(path, JSON.stringify(ids));
}

export function readSeenBuilds(
  pr: string
): Record<string, { status: string; attempts: number }> {
  const path = join(babysitDir(), `${pr}-seen-builds.json`);
  try {
    const text = require("fs").readFileSync(path, "utf-8");
    return JSON.parse(text) as Record<
      string,
      { status: string; attempts: number }
    >;
  } catch {
    return {};
  }
}

export function writeSeenBuilds(
  pr: string,
  builds: Record<string, { status: string; attempts: number }>
): void {
  ensureBabysitDir();
  const path = join(babysitDir(), `${pr}-seen-builds.json`);
  Bun.write(path, JSON.stringify(builds));
}

// ---------------------------------------------------------------------------
// Watch configuration
// ---------------------------------------------------------------------------

interface WatchConfig {
  repo: string;
  pr_number: string;
  branch: string;
  pipeline?: string;
  instructions?: string;
  no_comments?: boolean;
  no_builds?: boolean;
  commentInterval?: ReturnType<typeof setInterval>;
  buildInterval?: ReturnType<typeof setInterval>;
}

const watches = new Map<string, WatchConfig>();

// ---------------------------------------------------------------------------
// Polling stubs (filled in by Tasks 3 & 4)
// ---------------------------------------------------------------------------

async function pollComments(
  srv: Server,
  config: WatchConfig
): Promise<void> {
  try {
    // 1. Load seen comment IDs from state
    const seenIds = new Set(readSeenComments(config.pr_number));

    // 2. Fetch review comments via gh api
    const proc = Bun.spawn(
      [
        "gh",
        "api",
        `repos/${config.repo}/pulls/${config.pr_number}/comments`,
        "--paginate",
      ],
      { stdout: "pipe", stderr: "pipe" }
    );

    const stdout = await new Response(proc.stdout).text();
    const stderr = await new Response(proc.stderr).text();
    const exitCode = await proc.exited;

    if (exitCode !== 0) {
      console.error(
        `[babysit] pollComments: gh api failed (exit ${exitCode}): ${stderr}`
      );
      return;
    }

    let allComments: Array<{ id: number; body: string; [key: string]: unknown }>;
    try {
      allComments = JSON.parse(stdout);
    } catch (parseErr) {
      console.error(
        `[babysit] pollComments: failed to parse gh api JSON: ${parseErr}`
      );
      return;
    }

    if (!Array.isArray(allComments)) {
      console.error("[babysit] pollComments: expected array from gh api");
      return;
    }

    // 3. Filter to new comments not in the seen set
    const newComments = allComments.filter((c) => !seenIds.has(c.id));

    // 4. Skip self-authored comments (our bot replies use these callout patterns)
    const selfPatterns = ["> [!NOTE]\n> ### [", "> [!IMPORTANT]"];
    const externalComments = newComments.filter(
      (c) =>
        !selfPatterns.some(
          (pattern) => typeof c.body === "string" && c.body.includes(pattern)
        )
    );

    // 5. If there are new non-self comments, emit a channel notification
    if (externalComments.length > 0) {
      await srv.notification({
        method: "notifications/claude/channel",
        params: {
          content: JSON.stringify(externalComments),
          meta: {
            type: "new_comments",
            pr: config.pr_number,
            repo: config.repo,
            branch: config.branch,
            instructions: config.instructions || "",
          },
        },
      });
    }

    // 6. Update seen state with ALL comment IDs from this fetch
    const allIds = allComments.map((c) => c.id);
    const mergedIds = Array.from(new Set([...seenIds, ...allIds]));
    writeSeenComments(config.pr_number, mergedIds);
  } catch (err) {
    console.error(`[babysit] pollComments error: ${err}`);
  }
}

async function pollBuilds(
  _server: Server,
  _config: WatchConfig
): Promise<void> {
  // Stub — will be implemented in Task 4
}

// ---------------------------------------------------------------------------
// Watch / Unwatch logic
// ---------------------------------------------------------------------------

const POLL_INTERVAL_MS = 2 * 60 * 1000; // 2 minutes

function stopWatch(pr: string): boolean {
  const existing = watches.get(pr);
  if (!existing) return false;
  if (existing.commentInterval) clearInterval(existing.commentInterval);
  if (existing.buildInterval) clearInterval(existing.buildInterval);
  watches.delete(pr);
  return true;
}

function stopAllWatches(): string[] {
  const stopped: string[] = [];
  for (const pr of watches.keys()) {
    stopWatch(pr);
    stopped.push(pr);
  }
  return stopped;
}

function startWatch(server: Server, config: WatchConfig): void {
  // Only one active watch per session — stop any existing watch first
  stopAllWatches();

  const entry: WatchConfig = { ...config };

  if (!config.no_comments) {
    entry.commentInterval = setInterval(
      () => pollComments(server, entry),
      POLL_INTERVAL_MS
    );
  }

  if (!config.no_builds && config.pipeline) {
    entry.buildInterval = setInterval(
      () => pollBuilds(server, entry),
      POLL_INTERVAL_MS
    );
  }

  watches.set(config.pr_number, entry);
}

// ---------------------------------------------------------------------------
// Server
// ---------------------------------------------------------------------------

const server = new Server(
  { name: "babysit", version: "0.1.0" },
  {
    capabilities: {
      tools: {},
      experimental: {
        "claude/channel": {} as object,
      },
    },
    instructions: [
      "This is the babysit channel server. It monitors PRs for review comments and build failures.",
      "",
      "When you receive a <channel source=\"babysit\"> event, dispatch it based on the event type:",
      "",
      "- **comment** events: Dispatch to a sub-agent using the comment-check-prompt.md template.",
      "  Read the template from ${CLAUDE_SKILL_DIR}/assets/comment-check-prompt.md,",
      "  interpolate <REPO>, <PR_NUMBER>, and <BRANCH_NAME> from the watch config,",
      "  then pass the interpolated prompt to a sub-agent.",
      "",
      "- **build** events: Dispatch to a sub-agent using the build-check-prompt.md template.",
      "  Read the template from ${CLAUDE_SKILL_DIR}/assets/build-check-prompt.md,",
      "  interpolate <REPO>, <PR_NUMBER>, <BRANCH_NAME>, and <PIPELINE> from the watch config,",
      "  then pass the interpolated prompt to a sub-agent.",
      "",
      "If the watch config includes freeform `instructions`, append them to the interpolated",
      "prompt before dispatching to the sub-agent.",
    ].join("\n"),
  }
);

// ---------------------------------------------------------------------------
// Tool definitions
// ---------------------------------------------------------------------------

server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    {
      name: "watch",
      description:
        "Start monitoring a PR for review comments and/or build failures. Only one watch is active at a time — calling watch again replaces the current watch.",
      inputSchema: {
        type: "object" as const,
        properties: {
          repo: {
            type: "string",
            description:
              "Repository in owner/name format (e.g. 'acme/widgets')",
          },
          pr_number: {
            type: "string",
            description: "The PR number to monitor",
          },
          branch: {
            type: "string",
            description: "The head branch name of the PR",
          },
          pipeline: {
            type: "string",
            description:
              "Buildkite pipeline slug to monitor. Omit if --no-builds.",
          },
          instructions: {
            type: "string",
            description:
              "Freeform instructions to append to sub-agent prompts",
          },
          no_comments: {
            type: "boolean",
            description: "Disable comment monitoring",
          },
          no_builds: {
            type: "boolean",
            description: "Disable build monitoring",
          },
        },
        required: ["repo", "pr_number", "branch"],
      },
    },
    {
      name: "unwatch",
      description:
        "Stop monitoring a PR. If pr_number is omitted, stops all watches.",
      inputSchema: {
        type: "object" as const,
        properties: {
          pr_number: {
            type: "string",
            description:
              "The PR number to stop watching. Omit to stop all watches.",
          },
        },
      },
    },
    {
      name: "clean",
      description:
        "Remove stale state files for merged/closed PRs.",
      inputSchema: {
        type: "object" as const,
        properties: {},
      },
    },
  ],
}));

// ---------------------------------------------------------------------------
// Tool dispatch
// ---------------------------------------------------------------------------

server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const { name, arguments: args } = request.params;

  switch (name) {
    // ----- watch -----
    case "watch": {
      const repo = args?.repo as string;
      const pr_number = args?.pr_number as string;
      const branch = args?.branch as string;
      const pipeline = args?.pipeline as string | undefined;
      const instructions = args?.instructions as string | undefined;
      const no_comments = args?.no_comments as boolean | undefined;
      const no_builds = args?.no_builds as boolean | undefined;

      if (!repo || !pr_number || !branch) {
        return {
          content: [
            {
              type: "text" as const,
              text: "Error: repo, pr_number, and branch are required.",
            },
          ],
          isError: true,
        };
      }

      const config: WatchConfig = {
        repo,
        pr_number,
        branch,
        pipeline,
        instructions,
        no_comments,
        no_builds,
      };

      startWatch(server, config);

      const monitoring: string[] = [];
      if (!no_comments) monitoring.push("comments");
      if (!no_builds && pipeline) monitoring.push("builds");

      return {
        content: [
          {
            type: "text" as const,
            text: [
              `Watching PR #${pr_number} on ${repo} (branch: ${branch})`,
              `Monitoring: ${monitoring.join(", ") || "nothing (both disabled)"}`,
              `Polling interval: ${POLL_INTERVAL_MS / 1000}s`,
              pipeline ? `Pipeline: ${pipeline}` : null,
              instructions ? `Custom instructions: ${instructions}` : null,
            ]
              .filter(Boolean)
              .join("\n"),
          },
        ],
      };
    }

    // ----- unwatch -----
    case "unwatch": {
      const pr = args?.pr_number as string | undefined;

      if (pr) {
        const stopped = stopWatch(pr);
        return {
          content: [
            {
              type: "text" as const,
              text: stopped
                ? `Stopped watching PR #${pr}.`
                : `No active watch found for PR #${pr}.`,
            },
          ],
        };
      }

      const stopped = stopAllWatches();
      return {
        content: [
          {
            type: "text" as const,
            text:
              stopped.length > 0
                ? `Stopped watching PR(s): ${stopped.map((p) => `#${p}`).join(", ")}.`
                : "No active watches to stop.",
          },
        ],
      };
    }

    // ----- clean -----
    case "clean": {
      const dir = babysitDir();

      // If the babysit directory doesn't exist, nothing to clean
      if (!existsSync(dir)) {
        return {
          content: [
            {
              type: "text" as const,
              text: "No babysit state files found.",
            },
          ],
        };
      }

      // List state files and extract unique PR numbers
      const files = readdirSync(dir);
      const stateFiles = files.filter(
        (f) =>
          f.endsWith("-seen-comments.json") || f.endsWith("-seen-builds.json")
      );

      if (stateFiles.length === 0) {
        return {
          content: [
            {
              type: "text" as const,
              text: "No babysit state files found.",
            },
          ],
        };
      }

      const prNumbers = new Set<string>();
      for (const f of stateFiles) {
        // Filenames are like "123-seen-comments.json" or "123-seen-builds.json"
        const match = f.match(/^(\d+)-seen-(?:comments|builds)\.json$/);
        if (match) {
          prNumbers.add(match[1]);
        }
      }

      const cleaned: string[] = [];
      const preserved: string[] = [];

      for (const pr of prNumbers) {
        let state: string;
        try {
          state = execSync(`gh pr view ${pr} --json state --jq .state`, {
            encoding: "utf-8",
            timeout: 15_000,
          }).trim();
        } catch {
          // If gh fails (e.g. PR not found), treat as closed to allow cleanup
          state = "CLOSED";
        }

        if (state === "MERGED" || state === "CLOSED") {
          const commentsFile = join(dir, `${pr}-seen-comments.json`);
          const buildsFile = join(dir, `${pr}-seen-builds.json`);
          try {
            unlinkSync(commentsFile);
          } catch {
            // file may not exist
          }
          try {
            unlinkSync(buildsFile);
          } catch {
            // file may not exist
          }
          cleaned.push(`PR #${pr} (${state})`);
        } else {
          preserved.push(`PR #${pr} (still open)`);
        }
      }

      const lines: string[] = [];
      if (cleaned.length > 0) {
        lines.push("Cleaned state for:");
        for (const c of cleaned) lines.push(`  - ${c}`);
      }
      if (preserved.length > 0) {
        lines.push("Preserved state for:");
        for (const p of preserved) lines.push(`  - ${p}`);
      }
      if (cleaned.length === 0 && preserved.length === 0) {
        lines.push("No PR state files matched expected patterns.");
      }

      return {
        content: [
          {
            type: "text" as const,
            text: lines.join("\n"),
          },
        ],
      };
    }

    default:
      return {
        content: [
          {
            type: "text" as const,
            text: `Unknown tool: ${name}`,
          },
        ],
        isError: true,
      };
  }
});

// ---------------------------------------------------------------------------
// Start
// ---------------------------------------------------------------------------

async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
  console.error("[babysit] Channel server started");
}

main().catch((err) => {
  console.error("[babysit] Fatal error:", err);
  process.exit(1);
});
