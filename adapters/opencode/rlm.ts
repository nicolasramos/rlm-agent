/**
 * rlm — RLM (Recursive Language Model) plugin for OpenCode
 *
 * Implements the RLM paradigm (arXiv:2512.24601) for OpenCode:
 *
 *   1. Persistent Python kernel — the `ipython` tool executes code in a
 *      durable CPython REPL subprocess. Variables, imports, functions and
 *      results survive across tool calls, so the model keeps working state
 *      in the kernel instead of re-reading files or re-sending data through
 *      the LLM context on every turn ("no saturar").
 *   2. Subagents as native calls — the `rlm` tool spawns a background child
 *      session and returns an admission handle immediately; results arrive
 *      later via `rlm_result`. Children inherit the parent model and tools.
 *   3. State outliving turns — `rlm_snapshot` / `rlm_restore` persist the
 *      kernel namespace to disk; the compaction hook snapshots automatically
 *      and injects a summary of kernel variables into the compaction prompt.
 *   4. Context lake — `rlm_store` / `rlm_get` / `rlm_search` / `rlm_find`
 *      keep large data in a per-project store that never enters the prompt.
 *
 * The kernel is a self-contained Python runtime (kernel/kernel.py) speaking
 * newline-delimited JSON over stdio.
 *
 * Install: copy this file to ~/.config/opencode/plugins/rlm.ts and
 * kernel/kernel.py to ~/.config/opencode/rlm-kernel/kernel.py
 * (or set RLM_KERNEL to the kernel.py path).
 */

import type { Plugin } from "@opencode-ai/plugin"
import { tool } from "@opencode-ai/plugin"
import { createHash } from "crypto"
import { homedir } from "os"
import path from "path"

// ─── Configuration ───────────────────────────────────────────────────────────

const RLM_MAX_DEPTH = 2            // root → child → grandchild
const DEFAULT_TIMEOUT = 120        // seconds per ipython cell
const HARD_TIMEOUT = 300_000       // ms; JS-side backstop for a hung kernel
const MAX_CHILDREN_PER_SESSION = 8

function kernelPath(): string {
  if (process.env.RLM_KERNEL) return process.env.RLM_KERNEL
  const candidates = [
    path.join(homedir(), ".config", "opencode", "rlm-kernel", "kernel.py"),
    path.join(import.meta.dir, "..", "rlm-kernel", "kernel.py"),
    path.join(import.meta.dir, "kernel.py"),
  ]
  for (const c of candidates) {
    try {
      if (Bun.file(c).exists()) return c
    } catch {}
  }
  return candidates[0]
}

function pythonBin(): string {
  if (process.env.RLM_KERNEL_PYTHON) return process.env.RLM_KERNEL_PYTHON
  const candidates = ["python3", "/opt/homebrew/bin/python3.11", "/usr/local/bin/python3", "/usr/bin/python3"]
  for (const c of candidates) {
    try {
      const r = Bun.spawnSync([c, "--version"], { stdout: "ignore", stderr: "ignore" })
      if (r.exitCode === 0) return c
    } catch {}
  }
  return "python3"
}

function stateDirFor(sessionID: string): string {
  return path.join(homedir(), ".config", "opencode", "rlm-state", sessionID)
}

function lakeFileFor(projectDir: string): string {
  const hash = createHash("sha256").update(projectDir).digest("hex").slice(0, 16)
  return path.join(homedir(), ".config", "opencode", "rlm-state", "lake", `${hash}.jsonl`)
}

function unwrap<T>(res: any): T {
  return (res?.data ?? res) as T
}

// ─── Kernel process (JSON-lines over stdio) ─────────────────────────────────

interface PendingRequest {
  resolve: (ev: any) => void
  reject: (err: Error) => void
  stdout: string[]
  stderr: string[]
  timer: ReturnType<typeof setTimeout>
}

class Kernel {
  private proc: import("bun").Subprocess
  private buffer = ""
  private pending = new Map<string, PendingRequest>()
  private nextId = 0
  private stderrTail: string[] = []
  private closed = false
  readonly sessionID: string
  readonly directory: string
  readonly ready: Promise<void>

  constructor(python: string, kernelFile: string, sessionID: string, directory: string, lakeFile?: string) {
    this.sessionID = sessionID
    this.directory = directory
    this.proc = Bun.spawn([python, kernelFile], {
      stdin: "pipe",
      stdout: "pipe",
      stderr: "pipe",
      cwd: directory,
      env: lakeFile ? { ...process.env, RLM_LAKE_FILE: lakeFile } : process.env,
    })
    this.ready = new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("kernel did not announce ready")), 10_000)
      this.onReady = () => {
        clearTimeout(timer)
        resolve()
      }
      this.onExit = (err: Error) => {
        clearTimeout(timer)
        reject(err)
      }
    })
    this.readLoop()
    this.readStderr()
  }

  private onReady: (() => void) | null = null
  private onExit: ((err: Error) => void) | null = null

  private readLoop() {
    const reader = this.proc.stdout.getReader()
    const decoder = new TextDecoder()
    const pump = async () => {
      try {
        for (;;) {
          const { done, value } = await reader.read()
          if (done) break
          this.buffer += decoder.decode(value, { stream: true })
          let idx: number
          while ((idx = this.buffer.indexOf("\n")) >= 0) {
            const line = this.buffer.slice(0, idx)
            this.buffer = this.buffer.slice(idx + 1)
            if (line.trim()) this.handleLine(line)
          }
        }
      } catch {}
      this.failAll(new Error("kernel process exited"))
      this.onExit?.(new Error("kernel process exited"))
      this.onExit = null
    }
    pump()
  }

  private readStderr() {
    const reader = this.proc.stderr.getReader()
    const decoder = new TextDecoder()
    const pump = async () => {
      try {
        for (;;) {
          const { done, value } = await reader.read()
          if (done) break
          const text = decoder.decode(value, { stream: true })
          this.stderrTail.push(text)
          if (this.stderrTail.length > 20) this.stderrTail.shift()
        }
      } catch {}
    }
    pump()
  }

  private handleLine(line: string) {
    let ev: any
    try {
      ev = JSON.parse(line)
    } catch {
      return
    }
    if (ev.event === "ready") {
      this.onReady?.()
      this.onReady = null
      return
    }
    if (ev.event === "stdout" || ev.event === "stderr") {
      const p = this.pending.get(ev.id)
      if (p) (ev.event === "stdout" ? p.stdout : p.stderr).push(ev.text ?? "")
      return
    }
    if (ev.event === "result" || ev.event === "error" || ev.event === "names") {
      const p = this.pending.get(ev.id)
      if (p) {
        clearTimeout(p.timer)
        this.pending.delete(ev.id)
        p.resolve({ ...ev, stdout: p.stdout.join(""), stderr: p.stderr.join("") })
      }
    }
  }

  private failAll(err: Error) {
    this.closed = true
    for (const [, p] of this.pending) {
      clearTimeout(p.timer)
      p.reject(err)
    }
    this.pending.clear()
  }

  private request(type: string, payload: Record<string, unknown> = {}, timeoutMs = HARD_TIMEOUT): Promise<any> {
    if (this.closed) return Promise.reject(new Error("kernel is not running"))
    const id = `k${++this.nextId}`
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id)
        this.send({ id, type: "interrupt" })
        reject(new Error(`kernel request timed out after ${timeoutMs}ms`))
      }, timeoutMs)
      this.pending.set(id, { resolve, reject, stdout: [], stderr: [], timer })
      this.send({ id, type, ...payload })
    })
  }

  private send(req: Record<string, unknown>) {
    try {
      this.proc.stdin.write(JSON.stringify(req) + "\n")
    } catch (err) {
      this.failAll(err instanceof Error ? err : new Error(String(err)))
    }
  }

  async execute(code: string, timeout = DEFAULT_TIMEOUT): Promise<any> {
    const ev = await this.request("execute", { code, timeout })
    return {
      ok: ev.event === "result" && ev.ok !== false,
      result: ev.event === "result" ? ev : null,
      error: ev.event === "error" ? ev : null,
      stdout: ev.stdout ?? "",
      stderr: ev.stderr ?? "",
    }
  }

  async listNames(): Promise<any[]> {
    const ev = await this.request("list_names")
    return ev.names ?? []
  }

  async snapshot(file: string): Promise<any> {
    return this.request("snapshot", { path: file })
  }

  async restore(file: string): Promise<any> {
    return this.request("restore", { path: file })
  }

  async shutdown() {
    if (this.closed) return
    try {
      this.send({ id: `k${++this.nextId}`, type: "shutdown" })
      await Bun.sleep(300)
    } catch {}
    try {
      this.proc.kill()
    } catch {}
    this.closed = true
  }

  stderrTailText(): string {
    return this.stderrTail.join("").slice(-2000)
  }
}

// ─── Context Lake (context folding — RLM paper arXiv:2512.24601) ────────────
// The other half of RLM: context as an external resource accessed via tools.
// Large data stored here NEVER enters the LLM prompt; the model retrieves
// only what it needs with rlm_get / rlm_search / rlm_find.

const LAKE_CAPTURE_MIN_CHARS = 10_000   // auto-capture tool outputs above this
const LAKE_MAX_ENTRIES = 500            // hard cap on auto-captured entries
const LAKE_GET_MAX_CHARS = 50_000       // rlm_get returns at most this much

interface LakeEntry {
  key: string
  content: string
  tags: string[]
  source: "model" | "tool-capture" | "user"
  created: number
  updated: number
}

function escapeRegex(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")
}

class ContextLake {
  private file: string
  private entries = new Map<string, LakeEntry>()
  private loadPromise: Promise<void> | null = null
  private lastMtime = 0

  constructor(projectDir: string) {
    this.file = lakeFileFor(projectDir)
  }

  private async ensureLoaded(): Promise<void> {
    if (!this.loadPromise) this.loadPromise = this.load()
    await this.loadPromise
    // Reload when the file changed on disk (the kernel writes entries via
    // rlm_lake.store and must be visible to the plugin tools).
    try {
      const stat = await Bun.file(this.file).stat()
      if (stat.mtimeMs > this.lastMtime) {
        this.lastMtime = stat.mtimeMs
        await this.load()
      }
    } catch {}
  }

  private async load() {
    this.entries.clear()
    try {
      const f = Bun.file(this.file)
      if (await f.exists()) {
        const stat = await f.stat()
        this.lastMtime = stat.mtimeMs
        const text = await f.text()
        for (const line of text.split("\n")) {
          if (!line.trim()) continue
          try {
            const e = JSON.parse(line) as LakeEntry
            if (e?.key) this.entries.set(e.key, e)
          } catch {}
        }
      }
    } catch {}
  }

  private async append(entry: LakeEntry) {
    await Bun.write(this.file, JSON.stringify(entry) + "\n", { append: true })
  }

  private async rewrite() {
    const lines = [...this.entries.values()].map((e) => JSON.stringify(e)).join("\n")
    await Bun.write(this.file, lines ? lines + "\n" : "")
  }

  async store(
    key: string,
    content: string,
    tags: string[] = [],
    source: LakeEntry["source"] = "model"
  ): Promise<LakeEntry> {
    await this.ensureLoaded()
    const now = Date.now()
    const entry: LakeEntry = {
      key,
      content,
      tags,
      source,
      created: this.entries.get(key)?.created ?? now,
      updated: now,
    }
    this.entries.set(key, entry)
    await this.append(entry)
    return entry
  }

  async get(key: string): Promise<LakeEntry | undefined> {
    await this.ensureLoaded()
    return this.entries.get(key)
  }

  async search(pattern: string, maxResults = 10): Promise<LakeEntry[]> {
    await this.ensureLoaded()
    let re: RegExp
    try {
      re = new RegExp(pattern, "i")
    } catch {
      re = new RegExp(escapeRegex(pattern), "i")
    }
    const results: LakeEntry[] = []
    for (const e of this.entries.values()) {
      if (re.test(e.content) || re.test(e.key) || e.tags.some((t) => re.test(t))) {
        results.push(e)
        if (results.length >= maxResults) break
      }
    }
    return results
  }

  async find(text: string, maxResults = 10): Promise<LakeEntry[]> {
    await this.ensureLoaded()
    const needle = text.toLowerCase()
    const results: LakeEntry[] = []
    for (const e of this.entries.values()) {
      if (e.content.toLowerCase().includes(needle) || e.key.toLowerCase().includes(needle)) {
        results.push(e)
        if (results.length >= maxResults) break
      }
    }
    return results
  }

  async forget(pattern: string): Promise<number> {
    await this.ensureLoaded()
    let re: RegExp
    try {
      re = new RegExp(pattern, "i")
    } catch {
      re = new RegExp(escapeRegex(pattern), "i")
    }
    let removed = 0
    for (const [k, e] of this.entries) {
      if (re.test(e.key) || re.test(e.content)) {
        this.entries.delete(k)
        removed++
      }
    }
    if (removed > 0) await this.rewrite()
    return removed
  }

  async stats(): Promise<{ entries: number; chars: number; keys: string[] }> {
    await this.ensureLoaded()
    let chars = 0
    for (const e of this.entries.values()) chars += e.content.length
    return { entries: this.entries.size, chars, keys: [...this.entries.keys()] }
  }

  async captureIfLarge(toolName: string, callID: string, output: string, sessionID: string) {
    if (!output || output.length < LAKE_CAPTURE_MIN_CHARS) return
    await this.ensureLoaded()
    if (this.entries.size >= LAKE_MAX_ENTRIES) return
    const key = `auto:${toolName}:${createHash("sha256").update(callID).digest("hex").slice(0, 12)}`
    if (this.entries.has(key)) return
    await this.store(key, output.slice(0, 200_000), [toolName, `session:${sessionID}`], "tool-capture")
  }
}

function snippet(entry: LakeEntry, needle: string, width = 160): string {
  const idx = entry.content.toLowerCase().indexOf(needle.toLowerCase())
  if (idx < 0) return entry.content.slice(0, width)
  const start = Math.max(0, idx - width / 2)
  return (start > 0 ? "…" : "") + entry.content.slice(start, start + width) + "…"
}

// ─── Plugin ──────────────────────────────────────────────────────────────────

const RLM_INSTRUCTIONS = `## RLM Programming Model (persistent Python kernel + context lake)

You have an RLM-style persistent Python kernel via the \`ipython\` tool:

- State (variables, imports, functions, parsed results) SURVIVES across tool calls. Keep working state in the kernel instead of re-reading files or re-sending data into context on every turn.
- Use \`%%bash\` cells for shell commands; \`%cd <dir>\` changes the kernel's working directory persistently.
- Output is capped and concise on purpose: query specific values with small cells instead of dumping large data into the conversation.
- After heavy data work, call \`rlm_snapshot\` so the state survives compaction; after a compaction, call \`rlm_restore\` to reload it.

Context lake (context folding — data that never enters the prompt):

- \`rlm_store\` saves large data (outputs, reference material, logs) to a persistent per-project lake. Stored data is NOT in your context.
- \`rlm_search\` (regex) and \`rlm_find\` (text) locate entries and return only snippets; \`rlm_get\` loads a full entry by key; \`rlm_stats\` lists keys; \`rlm_forget\` deletes.
- FROM THE KERNEL: \`rlm_lake.store(key, data)\` writes a kernel variable straight into the lake (no prompt round-trip — ideal for big data); \`rlm_lake.get/search/find/stats/forget\` mirror the tools.
- Large tool outputs (>10KB) are auto-captured into the lake under \`auto:<tool>:<hash>\` keys — search them instead of re-running the tool.
- Prefer storing big data in the kernel or the lake over printing it into the conversation.

Subagents (RLM children):

- \`rlm\` spawns a background subagent and returns an admission handle immediately — do NOT wait for it. Continue your work; check results later with \`rlm_result\`.
- \`rlm_list\` shows the children of the current session; \`rlm_delete\` removes one.
- Children inherit your model, tools and skills. Recursion depth is limited to 2.`

export const Rlm: Plugin = async ({ client, directory }) => {
  const kernels = new Map<string, Kernel>()
  const children = new Map<string, any[]>()   // sessionID -> child entries
  const parents = new Map<string, string>()   // sessionID -> parentID
  const lakes = new Map<string, ContextLake>() // projectDir -> lake

  function lakeFor(projectDir: string): ContextLake {
    let lake = lakes.get(projectDir)
    if (!lake) {
      lake = new ContextLake(projectDir)
      lakes.set(projectDir, lake)
    }
    return lake
  }

  function depthOf(sessionID: string): number {
    let depth = 1
    let cur = sessionID
    const seen = new Set<string>()
    while (cur && parents.has(cur) && !seen.has(cur)) {
      seen.add(cur)
      cur = parents.get(cur)!
      depth++
    }
    return depth
  }

  async function getKernel(sessionID: string, cwd: string): Promise<Kernel> {
    let k = kernels.get(sessionID)
    if (k) return k
    k = new Kernel(pythonBin(), kernelPath(), sessionID, cwd, lakeFileFor(cwd))
    kernels.set(sessionID, k)
    try {
      await k.ready
    } catch (err) {
      kernels.delete(sessionID)
      throw new Error(
        `RLM kernel failed to start (python=${pythonBin()}, kernel=${kernelPath()}): ` +
          `${err instanceof Error ? err.message : String(err)}. ` +
          `stderr: ${k.stderrTailText() || "(empty)"}`
      )
    }
    return k
  }

  function cleanupSession(sessionID: string) {
    const k = kernels.get(sessionID)
    if (k) {
      k.shutdown().catch(() => {})
      kernels.delete(sessionID)
    }
    children.delete(sessionID)
    parents.delete(sessionID)
  }

  function registerChild(sessionID: string, entry: any) {
    const list = children.get(sessionID) ?? []
    list.push(entry)
    children.set(sessionID, list)
  }

  function findChild(sessionID: string, selector: string): any | undefined {
    const list = children.get(sessionID) ?? []
    const clean = (s: string) => s.trim().replace(/^['"]+|['"]+$/g, "")
    const target = clean(selector)
    return (
      list.find((c) => c.rlm_child_id === target) ??
      list.find((c) => clean(c.name) === target) ??
      list.find((c) => clean(c.session_name) === target) ??
      list.find((c) => clean(c.name).includes(target) || target.includes(clean(c.name)))
    )
  }

  function formatExecute(res: any): string {
    if (res.error) {
      const tb = (res.error.traceback ?? []).join("").trim()
      return `Error (${res.error.ename}): ${res.error.evalue}${tb ? `\n${tb}` : ""}`
    }
    const parts: string[] = []
    if (res.stdout) parts.push(res.stdout.trimEnd())
    if (res.result?.repr) parts.push(`→ ${res.result.repr}`)
    return parts.join("\n") || "(no output)"
  }

  return {
    tool: {
      ipython: tool({
        description:
          "Execute Python code in a persistent kernel. Variables, imports, functions and results survive across calls. " +
          "Use %%bash for shell commands and %cd to change the kernel's working directory. " +
          "Output is capped and concise: query specific values with small cells instead of dumping data.",
        args: {
          code: tool.schema.string().describe("Python code to execute in the persistent kernel"),
          timeout: tool.schema
            .number()
            .optional()
            .describe("Max seconds before the cell is interrupted (default 120)"),
        },
        async execute(args, context) {
          const kernel = await getKernel(context.sessionID, context.directory)
          const res = await kernel.execute(args.code, args.timeout ?? DEFAULT_TIMEOUT)
          return formatExecute(res)
        },
      }),

      rlm: tool({
        description:
          "Spawn a background subagent (RLM child) and return an admission handle immediately. " +
          "The child runs independently with its own context; results arrive later via rlm_result. " +
          "Children inherit the parent model, tools and skills. Depth is limited to 2.",
        args: {
          prompt: tool.schema.string().describe("Task for the subagent"),
          name: tool.schema.string().optional().describe("Readable child session name"),
          model: tool.schema
            .string()
            .optional()
            .describe("Exact provider/model selector for the child (default: inherit parent)"),
        },
        async execute(args, context) {
          const depth = depthOf(context.sessionID)
          if (depth >= RLM_MAX_DEPTH) {
            return `Cannot spawn RLM child: recursion depth limit (${RLM_MAX_DEPTH}) reached for this session.`
          }
          const list = children.get(context.sessionID) ?? []
          if (list.length >= MAX_CHILDREN_PER_SESSION) {
            return `Cannot spawn RLM child: session already has ${MAX_CHILDREN_PER_SESSION} children. Delete one with rlm_delete first.`
          }
          const title = (args.name ?? args.prompt.slice(0, 60))
            .trim()
            .replace(/^['"]+|['"]+$/g, "")
            .slice(0, 80)
          const created = await client.session.create({
            body: { parentID: context.sessionID, title },
            query: { directory: context.directory },
          })
          const child = unwrap<any>(created)
          const childId = child.id
          const promptBody: any = {
            parts: [{ type: "text", text: args.prompt }],
          }
          if (args.model) {
            const [providerID, modelID] = args.model.split("/")
            if (providerID && modelID) promptBody.model = { providerID, modelID }
          }
          // Fire-and-forget: the child processes in the background while we
          // return the admission handle immediately (RLM semantics). Do NOT
          // use noReply:true — that adds the message as context only and the
          // child session never runs.
          client.session
            .prompt({ path: { id: childId }, body: promptBody })
            .catch((err: unknown) => {
              console.error("[rlm] child prompt failed:", err)
            })
          const entry = {
            rlm_child_id: childId,
            name: title,
            session_name: title,
            session_dir: context.directory,
            model: args.model ?? "inherit",
            status: "running",
            created: Date.now(),
          }
          registerChild(context.sessionID, entry)
          return JSON.stringify({
            rlm_child_id: childId,
            name: title,
            session_dir: context.directory,
            model: args.model ?? "inherit",
            status: "running",
          })
        },
      }),

      rlm_list: tool({
        description: "List the RLM children (subagents) spawned by the current session.",
        args: {},
        async execute(_args, context) {
          const list = children.get(context.sessionID) ?? []
          if (list.length === 0) return "No RLM children in this session."
          return list
            .map(
              (c, i) =>
                `${i + 1}. ${c.name} — ${c.rlm_child_id} — ${c.status} — model: ${c.model}`
            )
            .join("\n")
        },
      }),

      rlm_result: tool({
        description:
          "Get the result of a previously spawned RLM child (by child id or name). " +
          "Returns {status: running|completed|error, text} — call again later if still running.",
        args: {
          child: tool.schema.string().describe("RLM child id or name from rlm_list"),
        },
        async execute(args, context) {
          const child = findChild(context.sessionID, args.child)
          if (!child) {
            return `No RLM child found for selector "${args.child}". Use rlm_list to see children of this session.`
          }
          const res = await client.session.messages({
            path: { id: child.rlm_child_id },
            query: { limit: 20 },
          })
          const msgs = unwrap<any[]>(res) ?? []
          const lastAssistant = [...msgs].reverse().find((m) => m.info?.role === "assistant")
          if (!lastAssistant) {
            return JSON.stringify({ status: "running", detail: "no assistant message yet" })
          }
          const info = lastAssistant.info ?? {}
          if (info.error) {
            return JSON.stringify({
              status: "error",
              error: info.error?.data?.message ?? JSON.stringify(info.error),
            })
          }
          const text = (lastAssistant.parts ?? [])
            .filter((p: any) => p.type === "text")
            .map((p: any) => p.text ?? "")
            .join("\n")
            .trim()
          const completed = !!info.time?.completed
          return JSON.stringify({ status: completed ? "completed" : "running", text })
        },
      }),

      rlm_delete: tool({
        description: "Delete a spawned RLM child (by child id or name). Cancels its session.",
        args: {
          child: tool.schema.string().describe("RLM child id or name from rlm_list"),
        },
        async execute(args, context) {
          const child = findChild(context.sessionID, args.child)
          if (!child) {
            return `No RLM child found for selector "${args.child}".`
          }
          try {
            await client.session.delete({ path: { id: child.rlm_child_id } })
          } catch (err) {
            return `Failed to delete child session: ${err instanceof Error ? err.message : String(err)}`
          }
          const list = children.get(context.sessionID) ?? []
          children.set(
            context.sessionID,
            list.filter((c) => c.rlm_child_id !== child.rlm_child_id)
          )
          return `Deleted RLM child ${child.name} (${child.rlm_child_id}).`
        },
      }),

      rlm_snapshot: tool({
        description:
          "Snapshot the persistent kernel state (variables, imports, functions) to disk. " +
          "Use before compaction or when you want to persist working state.",
        args: {},
        async execute(_args, context) {
          const kernel = kernels.get(context.sessionID)
          if (!kernel) return "No kernel state to snapshot (ipython not used yet)."
          const file = path.join(stateDirFor(context.sessionID), "kernel-state.pkl")
          const ev = await kernel.snapshot(file)
          if (ev.event === "error") {
            return `Snapshot failed: ${ev.evalue}`
          }
          return `Snapshot saved to ${file} — ${ev.names?.length ?? 0} variables (${ev.bytes ?? 0} bytes).`
        },
      }),

      rlm_restore: tool({
        description:
          "Restore the persistent kernel state from the last snapshot. " +
          "Call after a compaction or a fresh session to reload working variables.",
        args: {},
        async execute(_args, context) {
          const file = path.join(stateDirFor(context.sessionID), "kernel-state.pkl")
          if (!Bun.file(file).exists()) {
            return `No snapshot found at ${file}. Use rlm_snapshot first.`
          }
          const kernel = await getKernel(context.sessionID, context.directory)
          const ev = await kernel.restore(file)
          if (ev.event === "error") {
            return `Restore failed: ${ev.evalue}`
          }
          return `Restored ${ev.names?.length ?? 0} variables from ${file}.`
        },
      }),

      // ── Context lake tools (context folding — RLM paper) ──────────────

      rlm_store: tool({
        description:
          "Store a context entry in the persistent context lake. Data stored here does NOT enter the LLM prompt — " +
          "retrieve it later with rlm_get / rlm_search / rlm_find. Use for large outputs, reference data, or anything " +
          "you want available later without re-sending it into context.",
        args: {
          key: tool.schema.string().describe("Unique key for the entry"),
          content: tool.schema.string().describe("Content to store (can be large)"),
          tags: tool.schema
            .array(tool.schema.string())
            .optional()
            .describe("Optional tags to help search"),
        },
        async execute(args, context) {
          const lake = lakeFor(context.directory)
          const entry = await lake.store(args.key, args.content, args.tags ?? [], "model")
          return `Stored "${args.key}" (${entry.content.length} chars, tags: ${entry.tags.join(",") || "none"}). ` +
            `It is NOT in the LLM context — retrieve with rlm_get / rlm_search / rlm_find.`
        },
      }),

      rlm_get: tool({
        description:
          "Retrieve a context entry from the context lake by exact key. Returns the stored content (truncated to 50KB).",
        args: {
          key: tool.schema.string().describe("Entry key (see rlm_stats for keys)"),
        },
        async execute(args, context) {
          const lake = lakeFor(context.directory)
          const entry = await lake.get(args.key)
          if (!entry) return `No entry "${args.key}" in the context lake. Use rlm_stats to list keys.`
          const content =
            entry.content.length > LAKE_GET_MAX_CHARS
              ? entry.content.slice(0, LAKE_GET_MAX_CHARS) +
                `\n...[truncated, ${entry.content.length} chars total; use rlm_search for targeted retrieval]...`
              : entry.content
          return `[${args.key}] (${entry.content.length} chars, updated ${new Date(entry.updated).toISOString()})\n${content}`
        },
      }),

      rlm_search: tool({
        description:
          "Regex-search the context lake. Returns matching entry keys with a snippet around the first match. " +
          "Use to find specific data without loading whole entries into context.",
        args: {
          pattern: tool.schema.string().describe("Regex pattern (case-insensitive)"),
          max_results: tool.schema.number().optional().describe("Max matches (default 10)"),
        },
        async execute(args, context) {
          const lake = lakeFor(context.directory)
          const results = await lake.search(args.pattern, args.max_results ?? 10)
          if (results.length === 0) return `No entries match /${args.pattern}/ in the context lake.`
          return results
            .map(
              (e, i) =>
                `${i + 1}. ${e.key} (${e.content.length} chars, tags: ${e.tags.join(",") || "none"})\n` +
                `   ${snippet(e, args.pattern)}`
            )
            .join("\n")
        },
      }),

      rlm_find: tool({
        description:
          "Find context lake entries containing an exact text (case-insensitive substring). " +
          "Returns matching keys with a snippet around the first occurrence.",
        args: {
          text: tool.schema.string().describe("Text to find"),
          max_results: tool.schema.number().optional().describe("Max matches (default 10)"),
        },
        async execute(args, context) {
          const lake = lakeFor(context.directory)
          const results = await lake.find(args.text, args.max_results ?? 10)
          if (results.length === 0) return `No entries contain "${args.text}" in the context lake.`
          return results
            .map(
              (e, i) =>
                `${i + 1}. ${e.key} (${e.content.length} chars)\n` + `   ${snippet(e, args.text)}`
            )
            .join("\n")
        },
      }),

      rlm_stats: tool({
        description: "Show context lake statistics: entry count, total chars, and all keys.",
        args: {},
        async execute(_args, context) {
          const lake = lakeFor(context.directory)
          const s = await lake.stats()
          if (s.entries === 0) return "Context lake is empty. Store data with rlm_store."
          return (
            `Context lake: ${s.entries} entries, ${s.chars.toLocaleString()} chars total.\n` +
            `Keys:\n${s.keys.map((k) => `- ${k}`).join("\n")}`
          )
        },
      }),

      rlm_forget: tool({
        description:
          "Delete context lake entries whose key or content matches a regex pattern. Returns the number removed.",
        args: {
          pattern: tool.schema.string().describe("Regex pattern (case-insensitive)"),
        },
        async execute(args, context) {
          const lake = lakeFor(context.directory)
          const removed = await lake.forget(args.pattern)
          return removed > 0
            ? `Removed ${removed} entr${removed === 1 ? "y" : "ies"} matching /${args.pattern}/.`
            : `No entries match /${args.pattern}/.`
        },
      }),
    },

    event: async ({ event }) => {
      if (event.type === "session.created") {
        const info = (event.properties as any)?.info
        if (info?.id && info?.parentID) parents.set(info.id, info.parentID)
      }
      if (event.type === "session.deleted") {
        const info = (event.properties as any)?.info
        if (info?.id) cleanupSession(info.id)
      }
    },

    // Auto-capture large tool outputs into the context lake so they stay
    // retrievable without ever re-entering the LLM prompt.
    "tool.execute.after": async (input, output) => {
      try {
        const text = typeof output?.output === "string" ? output.output : ""
        if (text.length >= LAKE_CAPTURE_MIN_CHARS) {
          const lake = lakeFor(directory)
          await lake.captureIfLarge(input.tool, input.callID, text, input.sessionID)
        }
      } catch {
        // Best-effort: capture must never break tool execution.
      }
    },

    "experimental.session.compacting": async (input, output) => {
      const kernel = kernels.get(input.sessionID)
      if (!kernel) return
      try {
        const names = await kernel.listNames()
        const file = path.join(stateDirFor(input.sessionID), "kernel-state.pkl")
        await kernel.snapshot(file)
        if (names.length > 0) {
          const lines = names
            .slice(0, 40)
            .map((n: any) => `- ${n.name} (${n.type}): ${n.repr}`)
          output.context.push(
            `## RLM kernel state\n` +
              `This session has a persistent Python kernel (ipython tool). ` +
              `Current variables:\n${lines.join("\n")}\n` +
              `The full state was snapshotted to ${file}. ` +
              `Use the ipython tool to inspect or continue working with this state; ` +
              `call rlm_restore if the kernel was restarted.`
          )
        }
      } catch {
        // Best-effort: compaction must never fail because of the kernel.
      }
    },

    "experimental.chat.system.transform": async (_input, output) => {
      output.system.push(RLM_INSTRUCTIONS)
    },

    dispose: async () => {
      for (const k of kernels.values()) {
        await k.shutdown().catch(() => {})
      }
      kernels.clear()
    },
  }
}

export default Rlm