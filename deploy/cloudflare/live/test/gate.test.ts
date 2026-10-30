// The hosted dashboard's gate, under plain Node: node --test --experimental-strip-types live/test/
// (tests/test_worker_gate.py runs it with the Python suite).
import assert from "node:assert/strict";
import { timingSafeEqual } from "node:crypto";
import { test } from "node:test";

import { MISSES_PER_HOUR, NETWORK_MISSES_PER_HOUR, admit, guessers, isJudge, refused, type Store } from "../src/gate.ts";

// The Workers runtime's constant-time compare, which Node's Web Crypto doesn't have.
Object.assign(crypto.subtle, {
	timingSafeEqual: (a: ArrayBuffer, b: ArrayBuffer) => timingSafeEqual(new Uint8Array(a), new Uint8Array(b)),
});

/** Durable Object storage, in memory. */
class Memory implements Store {
	data = new Map<string, unknown>();
	async get<T>(key: string) {
		return this.data.get(key) as T | undefined;
	}
	async put<T>(key: string, value: T) {
		this.data.set(key, value);
	}
	async list<T>({ prefix, end, limit }: { prefix: string; end: string; limit: number }) {
		const keys = [...this.data.keys()].filter((key) => key.startsWith(prefix) && key < end).sort().slice(0, limit);
		return new Map(keys.map((key) => [key, this.data.get(key) as T]));
	}
	async delete(keys: string[]) {
		return keys.filter((key) => this.data.delete(key)).length;
	}
}

const ENV = { JUDGE_PASSWORD: "correct horse", NEW_RUNS_PER_DAY: "2", NEW_PRS_PER_DAY: "1" };
const NOON = new Date("2026-12-10T12:00:00Z");

function request(path: string, { method = "POST", password, ip = "203.0.113.7" }: { method?: string; password?: string; ip?: string } = {}) {
	const headers: Record<string, string> = { "cf-connecting-ip": ip };
	if (password !== undefined) headers["x-openport-judge"] = password;
	return new Request(`https://nebius.narralogy.com${path}`, { method, headers });
}

const answer = (body: object, status = 200) => Response.json(body, { status });

test("a request that can do work is refused an encoded or doubled slash", () => {
	assert.ok(refused(request("/api%2Fruns")));
	assert.ok(refused(request("/api//runs")));
	assert.ok(refused(request("/api/runs/%61b/github", { method: "PUT" })));
	assert.ok(!refused(request("/api/runs")));
	assert.ok(!refused(request("/api%2Fruns", { method: "GET" }))); // reading does nothing
});

test("IPv6 guesses are counted per /64 and /48, however the address is written", () => {
	const full = guessers("2001:0db8:0000:0000:0000:0000:0000:0001");
	assert.deepEqual(full, { address: "2001:db8:0:0::/64", network: "2001:db8:0::/48" });
	assert.deepEqual(guessers("2001:db8::1"), full); // compressed
	assert.deepEqual(guessers("2001:DB8:0:0:ffff::2"), full); // another host in the same /64
	assert.equal(guessers("2001:db8:0:1::1").network, full.network); // another /64 of the same /48
	assert.notEqual(guessers("2001:db8:0:1::1").address, full.address);
	assert.equal(guessers("::").address, "0:0:0:0::/64");
	assert.equal(guessers("1:2:3:4:5:6:7::").address, "1:2:3:4::/64");
	assert.deepEqual(guessers("::ffff:198.51.100.9"), { address: "198.51.100.9", network: "198.51.100.0/24" });
	assert.deepEqual(guessers("198.51.100.9"), { address: "198.51.100.9", network: "198.51.100.0/24" });
	assert.equal(guessers("1::2::3").address, "1::2::3"); // not an address: counted as it came
});

test("the password lets a judge in; wrong guesses stop being checked past the hour's limit", async () => {
	const store = new Memory();
	assert.equal(await isJudge(request("/api/site", { password: "correct horse" }), ENV, store, NOON), true);
	assert.equal(await isJudge(request("/api/site"), ENV, store, NOON), false); // no password: not a guess
	assert.equal(store.data.size, 0);
	for (let n = 0; n < MISSES_PER_HOUR; n++) {
		assert.equal(await isJudge(request("/api/site", { password: `guess ${n}` }), ENV, store, NOON), false);
	}
	// Past the limit even the right password isn't checked from there, this hour.
	assert.equal(await isJudge(request("/api/site", { password: "correct horse" }), ENV, store, NOON), false);
	assert.equal(await isJudge(request("/api/site", { password: "correct horse", ip: "203.0.113.8" }), ENV, store, NOON), true);
	const nextHour = new Date("2026-12-10T13:00:00Z");
	assert.equal(await isJudge(request("/api/site", { password: "correct horse" }), ENV, store, nextHour), true);
	await isJudge(request("/api/site", { password: "wrong" }), ENV, store, nextHour);
	assert.ok([...store.data.keys()].every((key) => key.includes("T13")), "earlier hours' counts are dropped");
});

test("an IPv6 guesser can't get a fresh limit from each address of its network", async () => {
	const store = new Memory();
	for (let n = 0; n < NETWORK_MISSES_PER_HOUR; n++) {
		const ip = `2001:db8:7:${(n % 0xffff).toString(16)}::${n.toString(16)}`; // a new /64 each time, one /48
		assert.equal(await isJudge(request("/api/site", { password: "guess", ip }), ENV, store, NOON), false);
	}
	assert.equal(await isJudge(request("/api/site", { password: "correct horse", ip: "2001:db8:7:ffff::1" }), ENV, store, NOON), false);
	assert.equal(await isJudge(request("/api/site", { password: "correct horse", ip: "2001:db8:8::1" }), ENV, store, NOON), true);
});

test("with a password set, anything that can do work needs it, whatever its path", async () => {
	for (const [path, method] of [["/api/runs", "POST"], ["/api/upgrades", "POST"], ["/api/runs/ab12/github", "POST"],
		["/api/anything-new", "POST"], ["/api/runs/ab12", "DELETE"], ["/api/runs", "PUT"]]) {
		const admission = await admit(request(path, { method }), ENV, new Memory(), NOON);
		assert.equal(admission.reuseOnly, "judges", `${method} ${path}`);
	}
	assert.equal((await admit(request("/api/runs", { method: "GET" }), ENV, new Memory(), NOON)).reuseOnly, null);
	const open = { ...ENV, JUDGE_PASSWORD: undefined }; // no password: live runs for everyone, still counted
	assert.equal((await admit(request("/api/runs"), open, new Memory(), NOON)).reuseOnly, null);
});

test("a judge's run takes one of the day's slots, kept only if a new run started", async () => {
	const store = new Memory();
	const judge = () => request("/api/runs", { password: "correct horse" });
	const first = await admit(judge(), ENV, store, NOON);
	assert.equal(first.reuseOnly, null);
	await first.settle(answer({ reused: false }));
	const replay = await admit(judge(), ENV, store, NOON);
	await replay.settle(answer({ reused: true })); // showed a run it already had: the slot goes back
	const failed = await admit(judge(), ENV, store, NOON);
	await failed.settle(answer({ detail: "boom" }, 500));
	const lost = await admit(judge(), ENV, store, NOON);
	await lost.settle(null); // the container never answered
	assert.equal(store.data.get("new-runs:2026-12-10"), 1);
	const second = await admit(judge(), ENV, store, NOON);
	await second.settle(answer({ reused: false }));
	assert.equal((await admit(judge(), ENV, store, NOON)).reuseOnly, "1"); // the day's two are used
	assert.equal((await admit(judge(), ENV, store, new Date("2026-12-11T00:00:00Z"))).reuseOnly, null); // a new day
});

test("pull requests have their own count, kept only when one was opened", async () => {
	const store = new Memory();
	const opening = () => request("/api/runs/ab12/github", { password: "correct horse" });
	const already = await admit(opening(), ENV, store, NOON);
	await already.settle(answer({ opened: false, url: "https://github.com/x/y/pull/1" }));
	const opened = await admit(opening(), ENV, store, NOON);
	await opened.settle(answer({ opened: true }));
	assert.equal((await admit(opening(), ENV, store, NOON)).reuseOnly, "1");
	assert.equal(store.data.get("new-prs:2026-12-10"), 1);
	assert.equal(store.data.get("new-runs:2026-12-10"), undefined);
});

test("slots taken together are each counted, so arrivals at once can't pass the cap", async () => {
	const store = new Memory();
	const judge = () => request("/api/runs", { password: "correct horse" });
	const admitted = [];
	for (let n = 0; n < 3; n++) admitted.push(await admit(judge(), ENV, store, NOON)); // none has answered yet
	assert.deepEqual(admitted.map((a) => a.reuseOnly), [null, null, "1"]);
});
