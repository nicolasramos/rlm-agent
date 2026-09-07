/**
 * rlm — RLM (Recursive Language Model) extension for PI (pi-mono)
 *
 * Ports the RLM plugin for OpenCode to PI's extension API:
 *   - ipython: persistent Python kernel (reuses kernel/kernel.py as-is)
 *   - rlm_lake tools: context lake (same JSONL format as the OpenCode plugin)
 *   - rlm: background subagent via `pi --mode json "<prompt>"` subprocess
 *
 * Install: copy this file to ~/.pi/agent/extensions/rlm.ts (global) or
 * .pi/extensions/rlm.ts (project), and kernel/kernel.py to
 * ~/.pi/agent/rlm-kernel/kernel.py (or set RLM_KERNEL). Then /reload.
 *
 * Publish: npm package + https://pi.dev/packages (the PI plugin registry).
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent"
import { Type } from "typebox"
import { spawn, spawnSync } from "node:child_process"
import { createHash } from "node:crypto"
import { existsSync, readFileSync, writeFileSync, appendFileSync, mkdirSync, statSync } from "node:fs"
import { homedir } from "node:os"
import path from "node:path"
import readline from "node:readline"

// ─── Config ──────────────────────────────────────────────────────────────────

const DEFAULT_TIMEOUT = 120
const LAKE_GET_MAX_CHARS = 50_000

function kernelPath(): string {
  if (process.env.RLM_KERNEL) return process.env.RLM_KERNEL
  const candidates = [
    path.join(homedir(), ".pi", "agent", "rlm-kernel", "kernel.py"),
    path.join(__dirname, "kernel.py"),
    path.join(process.cwd(), "kernel.py"),
  ]
  return candidates.find((c) => existsSync(c)) ?? candidates[0]
}

function pythonBin(): string {
  if (process.env.RLM_KERNEL_PYTHON) return process.env.RLM_KERNEL_PYTHON
  for (const c of ["python3", "/opt/homebrew/bin/python3.11", "/usr/local/bin/python3", "/usr/bin/python3"]) {
    try {
      const r = spawnSync(c, ["--version"], { stdio: "ignore" })
      if (r.status === 0) return c
    } catch {}
  }
  return "python3"
}

function lakeFileFor(projectDir: string): string {
  const hash = createHash("sha256").update(projectDir).digest("hex").slice(0, 16)
  return path.join(homedir(), ".pi", "agent", "rlm-state", "lake", `${hash}.jsonl`)
}

// ─── Kernel client (JSON-lines over stdio, same protocol as kernel.py) ───────

interface Pending {
  resolve: (ev: any) => void
  reject: (err: Error) => void
  stdout: string[]
  stderr: string[]
}

class Kernel {
  private proc: ReturnType<typeof spawn>
  private buffer = ""
  private pending = new Map<string, Pending>()
  private nextId = 0
  private closed = false
  private stderrTail: string[] = []

  constructor(cwd: string) {
    this.proc = spawn(pythonBin(), [kernelPath()], {
      cwd,
      stdio: ["pipe", "pipe", "pipe"],
      env: { ...process.env, RLM_LAKE_FILE: lakeFileFor(cwd) },
    })
    const rl = readline.createInterface({ input: this.proc.stdout })
    rl.on("line", (line) => this.handleLine(line))
    this.proc.stderr.on("data", (d: Buffer) => {
      this.stderrTail.push(d.toString())
      if (this.stderrTail.length > 20) this.stderrTail.shift()
    })
    this.proc.on("exit", () => {
      this.closed = true
      for (const [, p] of this.pending) p.reject(new Error("kernel process exited"))
      this.pending.clear()
    })
  }

  private handleLine(line: string) {
    let ev: any
    try {
      ev = JSON.parse(line)
    } catch {
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
        this.pending.delete(ev.id)
        p.resolve({ ...ev, stdout: p.stdout.join(""), stderr: p.stderr.join("") })
      }
    }
  }

  private request(type: string, payload: Record<string, unknown> = {}, timeoutMs = 300_000): Promise<any> {
    if (this.closed) return Promise.reject(new Error("kernel is not running"))
    const id = `k${++this.nextId}`
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id)
        this.send({ id, type: "interrupt" })
        reject(new Error(`kernel request timed out after ${timeoutMs}ms`))
      }, timeoutMs)
      this.pending.set(id, { resolve, reject, stdout: [], stderr: [] })
      this.send({ id, type, ...payload })
      if (timer.unref) timer.unref()
    })
  }

  private send(req: Record<string, unknown>) {
    try {
      this.proc.stdin.write(JSON.stringify(req) + "\n")
    } catch (err) {
      this.closed = true
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

  async shutdown() {
    if (this.closed) return
    try {
      this.send({ id: `k${++this.nextId}`, type: "shutdown" })
      await new Promise((r) => setTimeout(r, 300))
    } catch {}
    try {
      this.proc.kill()
    } catch {}
    this.closed = true
  }
}

// ─── Context lake (same JSONL format as the OpenCode plugin) ────────────────

interface LakeEntry {
  key: string
  content: string
  tags: string[]
  source: string
  created: number
  updated: number
}

class ContextLake {
  private file: string
  private entries = new Map<string, LakeEntry>()
  private lastMtime = 0

  constructor(projectDir: string) {
    this.file = lakeFileFor(projectDir)
  }

  private ensure() {
    let mtime = 0
    try {
      mtime = existsSync(this.file) ? statSync(this.file).mtimeMs : 0
    } catch {}
    if (this.lastMtime > 0 && mtime <= this.lastMtime) return
    this.lastMtime = mtime
    this.entries.clear()
    if (!existsSync(this.file)) return
    for (const line of readFileSync(this.file, "utf-8").split("\n")) {
      if (!line.trim()) continue
      try {
        const e = JSON.parse(line) as LakeEntry
        if (e?.key) this.entries.set(e.key, e)
      } catch {}
    }
  }

  store(key: string, content: string, tags: string[] = [], source = "model"): LakeEntry {
    this.ensure()
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
    mkdirSync(path.dirname(this.file), { recursive: true })
    appendFileSync(this.file, JSON.stringify(entry) + "\n")
    return entry
  }

  get(key: string): LakeEntry | undefined {
    this.ensure()
    return this.entries.get(key)
  }

  search(pattern: string, maxResults = 10): LakeEntry[] {
    this.ensure()
    let re: RegExp
    try {
      re = new RegExp(pattern, "i")
    } catch {
      re = new RegExp(pattern.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "i")
    }
    const out: LakeEntry[] = []
    for (const e of this.entries.values()) {
      if (re.test(e.content) || re.test(e.key) || e.tags.some((t) => re.test(t))) {
        out.push(e)
        if (out.length >= maxResults) break
      }
    }
    return out
  }

  find(text: string, maxResults = 10): LakeEntry[] {
    this.ensure()
    const needle = text.toLowerCase()
    const out: LakeEntry[] = []
    for (const e of this.entries.values()) {
      if (e.content.toLowerCase().includes(needle) || e.key.toLowerCase().includes(needle)) {
        out.push(e)
        if (out.length >= maxResults) break
      }
    }
    return out
  }

  stats() {
    this.ensure()
    let chars = 0
    for (const e of this.entries.values()) chars += e.content.length
    return { entries: this.entries.size, chars, keys: [...this.entries.keys()] }
  }

  forget(pattern: string): number {
    this.ensure()
    let re: RegExp
    try {
      re = new RegExp(pattern, "i")
    } catch {
      re = new RegExp(pattern.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "i")
    }
    let removed = 0
    for (const [k, e] of this.entries) {
      if (re.test(e.key) || re.test(e.content)) {
        this.entries.delete(k)
        removed++
      }
    }
    if (removed > 0) {
      writeFileSync(this.file, [...this.entries.values()].map((e) => JSON.stringify(e)).join("\n") + "\n")
    }
    return removed
  }
}

function snippet(entry: LakeEntry, needle: string, width = 160): string {
  const idx = entry.content.toLowerCase().indexOf(needle.toLowerCase())
  if (idx < 0) return entry.content.slice(0, width)
  const start = Math.max(0, idx - width / 2)
  return (start > 0 ? "…" : "") + entry.content.slice(start, start + width) + "…"
}

// ─── Extension ───────────────────────────────────────────────────────────────

export default function (pi: ExtensionAPI) {
  const kernels = new Map<string, Kernel>()
  const lakes = new Map<string, ContextLake>()
  const children = new Map<string, any[]>()

  function lakeFor(projectDir: string): ContextLake {
    let lake = lakes.get(projectDir)
    if (!lake) {
      lake = new ContextLake(projectDir)
      lakes.set(projectDir, lake)
    }
    return lake
  }

  async function getKernel(cwd: string): Promise<Kernel> {
    let k = kernels.get(cwd)
    if (k) return k
    k = new Kernel(cwd)
    kernels.set(cwd, k)
    // Give the kernel a moment to announce ready; surface a clear error if not.
    await new Promise((r) => setTimeout(r, 400))
    if (k["closed"]) {
      kernels.delete(cwd)
      throw new Error(`RLM kernel failed to start (python=${pythonBin()}, kernel=${kernelPath()})`)
    }
    return k
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

  // ── ipython: persistent kernel ─────────────────────────────────────────────
  pi.registerTool({
    name: "ipython",
    label: "IPython",
    description:
      "Execute Python code in a persistent kernel. Variables, imports, functions and results survive across calls. " +
      "Use %%bash for shell commands and %cd to change the kernel's working directory. Output is capped and concise.",
    parameters: Type.Object({
      code: Type.String({ description: "Python code to execute in the persistent kernel" }),
      timeout: Type.Optional(Type.Number({ description: "Max seconds before the cell is interrupted (default 120)" })),
    }),
    async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
      try {
        const kernel = await getKernel(ctx.projectDir ?? process.cwd())
        const res = await kernel.execute(params.code, params.timeout ?? DEFAULT_TIMEOUT)
        return { content: [{ type: "text", text: formatExecute(res) }], details: {} }
      } catch (err) {
        return { content: [{ type: "text", text: `ipython error: ${err instanceof Error ? err.message : String(err)}` }], details: {} }
      }
    },
  })

  // ── Context lake tools ─────────────────────────────────────────────────────
  pi.registerTool({
    name: "rlm_store",
    label: "RLM Store",
    description:
      "Store a context entry in the persistent context lake. Data stored here does NOT enter the LLM prompt — " +
      "retrieve it later with rlm_get / rlm_search / rlm_find.",
    parameters: Type.Object({
      key: Type.String({ description: "Unique key for the entry" }),
      content: Type.String({ description: "Content to store (can be large)" }),
      tags: Type.Optional(Type.Array(Type.String(), { description: "Optional tags" })),
    }),
    async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
      const lake = lakeFor(ctx.projectDir ?? process.cwd())
      const entry = lake.store(params.key, params.content, params.tags ?? [], "model")
      return {
        content: [{ type: "text", text: `Stored "${params.key}" (${entry.content.length} chars). Not in the LLM context.` }],
        details: {},
      }
    },
  })

  pi.registerTool({
    name: "rlm_get",
    label: "RLM Get",
    description: "Retrieve a context entry from the context lake by exact key (truncated to 50KB).",
    parameters: Type.Object({ key: Type.String({ description: "Entry key" }) }),
    async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
      const lake = lakeFor(ctx.projectDir ?? process.cwd())
      const entry = lake.get(params.key)
      if (!entry) return { content: [{ type: "text", text: `No entry "${params.key}" in the context lake.` }], details: {} }
      const content =
        entry.content.length > LAKE_GET_MAX_CHARS
          ? entry.content.slice(0, LAKE_GET_MAX_CHARS) + "\n...[truncated]..."
          : entry.content
      return { content: [{ type: "text", text: `[${params.key}] (${entry.content.length} chars)\n${content}` }], details: {} }
    },
  })

  pi.registerTool({
    name: "rlm_search",
    label: "RLM Search",
    description: "Regex-search the context lake. Returns matching keys with a snippet around the first match.",
    parameters: Type.Object({
      pattern: Type.String({ description: "Regex pattern (case-insensitive)" }),
      max_results: Type.Optional(Type.Number({ description: "Max matches (default 10)" })),
    }),
    async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
      const lake = lakeFor(ctx.projectDir ?? process.cwd())
      const results = lake.search(params.pattern, params.max_results ?? 10)
      if (results.length === 0) return { content: [{ type: "text", text: `No entries match /${params.pattern}/.` }], details: {} }
      return {
        content: [
          {
            type: "text",
            text: results
              .map((e, i) => `${i + 1}. ${e.key} (${e.content.length} chars)\n   ${snippet(e, params.pattern)}`)
              .join("\n"),
          },
        ],
        details: {},
      }
    },
  })

  pi.registerTool({
    name: "rlm_find",
    label: "RLM Find",
    description: "Find context lake entries containing an exact text (case-insensitive substring).",
    parameters: Type.Object({
      text: Type.String({ description: "Text to find" }),
      max_results: Type.Optional(Type.Number({ description: "Max matches (default 10)" })),
    }),
    async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
      const lake = lakeFor(ctx.projectDir ?? process.cwd())
      const results = lake.find(params.text, params.max_results ?? 10)
      if (results.length === 0) return { content: [{ type: "text", text: `No entries contain "${params.text}".` }], details: {} }
      return {
        content: [
          {
            type: "text",
            text: results.map((e, i) => `${i + 1}. ${e.key} (${e.content.length} chars)\n   ${snippet(e, params.text)}`).join("\n"),
          },
        ],
        details: {},
      }
    },
  })

  pi.registerTool({
    name: "rlm_stats",
    label: "RLM Stats",
    description: "Show context lake statistics: entry count, total chars, and all keys.",
    parameters: Type.Object({}),
    async execute(_toolCallId, _params, _signal, _onUpdate, ctx) {
      const lake = lakeFor(ctx.projectDir ?? process.cwd())
      const s = lake.stats()
      if (s.entries === 0) return { content: [{ type: "text", text: "Context lake is empty. Store data with rlm_store." }], details: {} }
      return {
        content: [
          {
            type: "text",
            text: `Context lake: ${s.entries} entries, ${s.chars.toLocaleString()} chars total.\nKeys:\n${s.keys.map((k) => `- ${k}`).join("\n")}`,
          },
        ],
        details: {},
      }
    },
  })

  pi.registerTool({
    name: "rlm_forget",
    label: "RLM Forget",
    description: "Delete context lake entries whose key or content matches a regex pattern.",
    parameters: Type.Object({ pattern: Type.String({ description: "Regex pattern (case-insensitive)" }) }),
    async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
      const lake = lakeFor(ctx.projectDir ?? process.cwd())
      const removed = lake.forget(params.pattern)
      return {
        content: [{ type: "text", text: removed > 0 ? `Removed ${removed} entries matching /${params.pattern}/.` : `No entries match /${params.pattern}/.` }],
        details: {},
      }
    },
  })

  // ── rlm: background subagent via `pi --mode json "<prompt>"` ───────────────
  pi.registerTool({
    name: "rlm",
    label: "RLM Subagent",
    description:
      "Spawn a background subagent (pi --mode json) and return an admission handle immediately. " +
      "The child runs independently; results arrive later via rlm_result.",
    parameters: Type.Object({
      prompt: Type.String({ description: "Task for the subagent" }),
      name: Type.Optional(Type.String({ description: "Readable child name" })),
    }),
    async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
      const cwd = ctx.projectDir ?? process.cwd()
      const name = (params.name ?? params.prompt.slice(0, 40)).replace(/^['"]+|['"]+$/g, "").slice(0, 60)
      const outFile = path.join(homedir(), ".pi", "agent", "rlm-state", "children", `${Date.now()}-${Math.random().toString(36).slice(2, 8)}.jsonl`)
      mkdirSync(path.dirname(outFile), { recursive: true })
      const child = spawn("pi", ["--mode", "json", params.prompt], { cwd, stdio: ["ignore", "pipe", "pipe"] })
      const fs = require("node:fs") as typeof import("node:fs")
      child.stdout.on("data", (d: Buffer) => fs.appendFileSync(outFile, d))
      child.stderr.on("data", () => {})
      const entry = { rlm_child_id: outFile, name, status: "running", pid: child.pid, created: Date.now() }
      const list = children.get(cwd) ?? []
      list.push(entry)
      children.set(cwd, list)
      child.on("exit", () => {
        entry.status = "completed"
      })
      return {
        content: [{ type: "text", text: JSON.stringify({ rlm_child_id: outFile, name, status: "running" }) }],
        details: {},
      }
    },
  })

  pi.registerTool({
    name: "rlm_result",
    label: "RLM Result",
    description: "Get the result of a previously spawned RLM child (by id or name).",
    parameters: Type.Object({ child: Type.String({ description: "RLM child id or name" }) }),
    async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
      const cwd = ctx.projectDir ?? process.cwd()
      const list = children.get(cwd) ?? []
      const child = list.find((c) => c.rlm_child_id === params.child || c.name === params.child)
      if (!child) return { content: [{ type: "text", text: `No RLM child found for "${params.child}".` }], details: {} }
      if (child.status === "running") {
        return { content: [{ type: "text", text: JSON.stringify({ status: "running" }) }], details: {} }
      }
      const text = existsSync(child.rlm_child_id) ? readFileSync(child.rlm_child_id, "utf-8").slice(-4000) : ""
      return { content: [{ type: "text", text: JSON.stringify({ status: "completed", output_tail: text }) }], details: {} }
    },
  })

  pi.registerTool({
    name: "rlm_list",
    label: "RLM List",
    description: "List the RLM children spawned by this project.",
    parameters: Type.Object({}),
    async execute(_toolCallId, _params, _signal, _onUpdate, ctx) {
      const cwd = ctx.projectDir ?? process.cwd()
      const list = children.get(cwd) ?? []
      if (list.length === 0) return { content: [{ type: "text", text: "No RLM children." }], details: {} }
      return {
        content: [{ type: "text", text: list.map((c, i) => `${i + 1}. ${c.name} — ${c.status}`).join("\n") }],
        details: {},
      }
    },
  })

  // Cleanup kernels on exit
  pi.on("session_end", async () => {
    for (const k of kernels.values()) await k.shutdown().catch(() => {})
    kernels.clear()
  })
}