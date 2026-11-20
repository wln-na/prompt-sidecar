const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const vm = require('node:vm');
const crypto = require('node:crypto');
const { test } = require('node:test');

const pluginPath = path.join(__dirname, '../dsh-plugin/prompt-sidecar-bridge/index.js');
const source = fs.readFileSync(pluginPath, 'utf8')
    .replace(/^import [\s\S]*? from 'node:[^']+'\n/gm, '')
    .replace('export function apply(', 'function apply(');

function fixture(run) {
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'sidecar-token-test-'));
    try {
        run(path.join(directory, 'bridge.token'), directory);
    } finally {
        fs.rmSync(directory, { recursive: true, force: true });
    }
}

function startPlugin(tokenFile, overrides = {}) {
    let listening = false;
    const context = {
        ...fs, ...path, ...crypto, constants: fs.constants, homedir: os.homedir,
        createServer: () => ({
            on() {},
            listen() { listening = true; },
            close() {},
        }),
        ...overrides,
    };
    vm.createContext(context);
    vm.runInContext(source, context, { filename: pluginPath });
    context.apply({}, { tokenFile });
    assert.equal(listening, true);
}

test('existing public token is restricted to 0600 without rotating its value', () => {
    fixture((file) => {
        fs.writeFileSync(file, 'fixture-only-token\n', { mode: 0o644 });
        fs.chmodSync(file, 0o644);
        startPlugin(file);
        assert.equal(fs.statSync(file).mode & 0o777, 0o600);
        assert.equal(fs.readFileSync(file, 'utf8'), 'fixture-only-token\n');
    });
});

test('existing private token is preserved', () => {
    fixture((file) => {
        fs.writeFileSync(file, 'fixture-only-token\n', { mode: 0o600 });
        startPlugin(file);
        assert.equal(fs.statSync(file).mode & 0o777, 0o600);
        assert.equal(fs.readFileSync(file, 'utf8'), 'fixture-only-token\n');
    });
});

test('new token is generated in a missing parent with private permissions', () => {
    fixture((file, directory) => {
        const nested = path.join(directory, 'nested', 'bridge.token');
        startPlugin(nested);
        assert.equal(fs.statSync(nested).mode & 0o777, 0o600);
        assert.match(fs.readFileSync(nested, 'utf8'), /^[a-f0-9]{64}\n$/);
    });
});

test('empty public token is replaced and its permissions are restricted', () => {
    fixture((file) => {
        fs.writeFileSync(file, ' \n', { mode: 0o644 });
        fs.chmodSync(file, 0o644);
        startPlugin(file);
        assert.equal(fs.statSync(file).mode & 0o777, 0o600);
        assert.match(fs.readFileSync(file, 'utf8'), /^[a-f0-9]{64}\n$/);
    });
});

test('permission failure stops startup without replacing the token', () => {
    fixture((file) => {
        fs.writeFileSync(file, 'fixture-only-token\n', { mode: 0o644 });
        let closed = false;
        assert.throws(() => startPlugin(file, {
            fchmodSync() { throw new Error('fixture chmod denied'); },
            closeSync(fd) { closed = true; fs.closeSync(fd); },
        }), /fixture chmod denied/);
        assert.equal(closed, true);
        assert.equal(fs.readFileSync(file, 'utf8'), 'fixture-only-token\n');
    });
});

test('symlink token is rejected without changing its target', () => {
    fixture((file, directory) => {
        const target = path.join(directory, 'other-file');
        fs.writeFileSync(target, 'fixture-only-token\n', { mode: 0o644 });
        fs.chmodSync(target, 0o644);
        fs.symlinkSync(target, file);
        assert.throws(() => startPlugin(file));
        assert.equal(fs.statSync(target).mode & 0o777, 0o644);
        assert.equal(fs.readFileSync(target, 'utf8'), 'fixture-only-token\n');
    });
});
