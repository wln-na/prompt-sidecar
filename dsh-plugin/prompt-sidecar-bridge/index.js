/**
 * Prompt Sidecar bridge — a Host-only plugin that exposes a localhost HTTP
 * endpoint so the local Prompt Sidecar panel can queue or steer prompts into
 * DSH sessions through the SessionController prompt API.
 */
import { createServer } from 'node:http'
import { randomUUID, randomBytes } from 'node:crypto'
import {
    closeSync, constants, fchmodSync, fstatSync, ftruncateSync,
    mkdirSync, openSync, readFileSync, writeSync,
} from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { homedir } from 'node:os'

const VERSION = '1.0.0'
const MAX_BODY_BYTES = 2 * 1024 * 1024

function expandHome(value) {
    if (!value) return value
    if (value === '~') return homedir()
    if (value.startsWith('~/')) return join(homedir(), value.slice(2))
    return value
}

function ensureToken(tokenFile) {
    const target = resolve(expandHome(tokenFile))
    mkdirSync(dirname(target), { recursive: true })
    const fd = openSync(target, constants.O_RDWR | constants.O_CREAT | constants.O_NOFOLLOW, 0o600)
    try {
        if (!fstatSync(fd).isFile()) throw new Error('Token must be a regular file')
        fchmodSync(fd, 0o600)
        const existing = readFileSync(fd, 'utf8').trim()
        if (existing) return { token: existing, path: target }
        const token = randomBytes(32).toString('hex')
        ftruncateSync(fd, 0)
        writeSync(fd, `${token}\n`, 0, 'utf8')
        return { token, path: target }
    } finally {
        closeSync(fd)
    }
}

function readBody(req, limit = MAX_BODY_BYTES) {
    return new Promise((resolvePromise, reject) => {
        let size = 0
        const chunks = []
        req.on('data', (chunk) => {
            size += chunk.length
            if (size > limit) {
                reject(new Error('body too large'))
                req.destroy()
                return
            }
            chunks.push(chunk)
        })
        req.on('end', () => resolvePromise(Buffer.concat(chunks).toString('utf8')))
        req.on('error', reject)
    })
}

function bearerToken(req) {
    const header = req.headers.authorization || ''
    return header.startsWith('Bearer ') ? header.slice(7).trim() : ''
}

export function apply(ctx, config) {
    const port = Number(config?.port) > 0 ? Number(config.port) : 8766
    const tokenFile = config?.tokenFile || '~/.dsh/prompt-sidecar-bridge.token'
    const { token, path: tokenPath } = ensureToken(tokenFile)

    const log = (message) => {
        try {
            ctx.logger?.info?.(`[prompt-sidecar-bridge] ${message}`)
        } catch {
            // logging must never break request handling
        }
    }

    const resolveController = () => {
        const attempts = [
            () => ctx.sessionController,
            () => ctx.get?.('sessionController', false),
            () => ctx.reflect?.get?.('sessionController'),
        ]
        for (const attempt of attempts) {
            try {
                const service = attempt()
                if (service) return service
            } catch {
                // try the next lookup
            }
        }
        return undefined
    }

    const respondJson = (res, status, payload) => {
        const body = JSON.stringify(payload)
        res.writeHead(status, {
            'Content-Type': 'application/json; charset=utf-8',
            'Content-Length': Buffer.byteLength(body),
        })
        res.end(body)
    }

    const server = createServer(async (req, res) => {
        let url
        try {
            url = new URL(req.url || '/', 'http://127.0.0.1')
        } catch {
            respondJson(res, 400, { ok: false, error: 'invalid url' })
            return
        }

        if (req.method === 'GET' && url.pathname === '/health') {
            respondJson(res, 200, { ok: true, version: VERSION })
            return
        }

        if (req.method === 'POST' && url.pathname === '/prompt') {
            if (bearerToken(req) !== token) {
                respondJson(res, 401, { ok: false, error: 'invalid token' })
                return
            }
            let request
            try {
                request = JSON.parse(await readBody(req))
            } catch {
                respondJson(res, 400, { ok: false, error: 'invalid json body' })
                return
            }
            const sessionId = String(request.sessionId || '').trim()
            const message = String(request.message || '')
            const mode = request.mode === 'steer' ? 'steer' : 'queue'
            const clientTimeZone = typeof request.clientTimeZone === 'string' && request.clientTimeZone
                ? request.clientTimeZone
                : undefined
            if (!sessionId) {
                respondJson(res, 400, { ok: false, error: 'sessionId is required' })
                return
            }
            if (!message.trim()) {
                respondJson(res, 400, { ok: false, error: 'message is required' })
                return
            }
            const controller = resolveController()
            if (!controller) {
                respondJson(res, 503, { ok: false, error: 'sessionController service is not available yet' })
                return
            }
            const requestId = randomUUID()
            try {
                const value = await controller.prompt(
                    {
                        requestId,
                        sessionId,
                        mode,
                        content: [{ type: 'text', text: message }],
                        ...(clientTimeZone ? { clientTimeZone } : {}),
                    },
                    new AbortController().signal,
                )
                log(`prompt ${mode} accepted (${requestId})`)
                respondJson(res, 200, {
                    ok: true,
                    accepted: value?.accepted !== false,
                    requestId,
                    mode,
                    sessionId,
                })
            } catch (error) {
                const code = String(error?.code || '')
                const messageText = String(error?.message || error || 'prompt failed')
                log(`prompt failed: ${code || 'unknown error'}`)
                if (code === 'session/not-found') {
                    respondJson(res, 404, { ok: false, error: 'session not found' })
                    return
                }
                respondJson(res, 500, { ok: false, error: messageText })
            }
            return
        }

        respondJson(res, 404, { ok: false, error: 'not found' })
    })

    server.on('error', (error) => {
        if (error?.code === 'EADDRINUSE') {
            log(`port ${port} is already in use; another bridge instance is probably running`)
            return
        }
        log(`server error: ${error?.message || error}`)
    })

    server.listen(port, '127.0.0.1', () => {
        log(`listening on http://127.0.0.1:${port}`)
    })

    return () => {
        server.close()
    }
}
