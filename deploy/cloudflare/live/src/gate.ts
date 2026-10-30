// What a request to the hosted dashboard may do: the judges' password, the guesses at it, and the day's counts of new
// runs and pull requests. Nothing here needs the Workers runtime, so it's tested under plain Node (test/gate.test.ts).

/** The Durable Object storage calls the gate uses. */
export interface Store {
	get<T>(key: string): Promise<T | undefined>;
	put<T>(key: string, value: T): Promise<void>;
	list<T>(options: { prefix: string; end: string; limit: number }): Promise<Map<string, T>>;
	delete(keys: string[]): Promise<number>;
}

export interface GateEnv {
	// Set as a secret, live runs are for whoever has it (the hackathon's judges); everyone else sees the replays.
	JUDGE_PASSWORD?: string;
	NEW_RUNS_PER_DAY?: string;
	NEW_PRS_PER_DAY?: string;
}

// Requests that can start a run (and spend Token Factory and Tavily credit) rather than replay one.
const RUN_STARTS = new Set(["/api/runs", "/api/upgrades"]);
// Requests that open a run's pull request on GitHub.
const PR_OPENS = /^\/api\/runs\/[a-f0-9]+\/github$/;
// Wrong passwords one address may try in an hour, and one network (an IPv6 /48 or an IPv4 /24); past either, guesses
// from there aren't checked until the next hour. There's no cap across all addresses: it would let anyone lock the
// judges out, and a guessed password could spend no more than the day's budget.
export const MISSES_PER_HOUR = 20;
export const NETWORK_MISSES_PER_HOUR = 100;
const MISSES = "judge-misses:";

const sha256 = (text: string) => crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));

/** Whether two secrets match, taking the same time whatever they hold. */
async function same(given: string, secret: string): Promise<boolean> {
	const [a, b] = await Promise.all([sha256(given), sha256(secret)]);
	return crypto.subtle.timingSafeEqual(a, b);
}

/** Anything but GET and HEAD can do work: start a run, open a pull request. */
export const mutating = (request: Request) => request.method !== "GET" && request.method !== "HEAD";

/** The server routes the decoded path, so /api%2Fruns or /api//runs would reach a run start without matching the
 * checks below. The dashboard never sends either, so a request that can do work is refused for them. */
export function refused(request: Request): boolean {
	return mutating(request) && /%|\/\//.test(new URL(request.url).pathname);
}

/** An IPv6 address's eight groups, written in full (`2001:db8::1` is 2001:db8:0:0:0:0:0:1), or null if it isn't one. */
function ipv6Groups(ip: string): number[] | null {
	const halves = ip.toLowerCase().split("::");
	if (halves.length > 2) return null;
	const part = (text: string) => (text ? text.split(":") : []);
	const [head, tail] = [part(halves[0]), halves.length === 2 ? part(halves[1]) : []];
	const missing = 8 - head.length - tail.length;
	if (halves.length === 2 ? missing < 1 : missing !== 0) return null;
	const groups = [...head, ...Array<string>(halves.length === 2 ? missing : 0).fill("0"), ...tail];
	if (!groups.every((group) => /^[0-9a-f]{1,4}$/.test(group))) return null;
	return groups.map((group) => parseInt(group, 16));
}

/** Where wrong guesses are counted: the address (one IPv6 host commonly holds a whole /64) and its network. */
export function guessers(ip: string): { address: string; network: string } {
	const mapped = /^::ffff:(\d{1,3}(?:\.\d{1,3}){3})$/i.exec(ip); // IPv4 written as IPv6
	const v4 = mapped ? mapped[1] : /^\d{1,3}(?:\.\d{1,3}){3}$/.test(ip) ? ip : null;
	if (v4) return { address: v4, network: `${v4.split(".").slice(0, 3).join(".")}.0/24` };
	const groups = ipv6Groups(ip);
	if (!groups) return { address: ip, network: ip }; // not an address: counted as given
	const prefix = (count: number) => groups.slice(0, count).map((group) => group.toString(16)).join(":");
	return { address: `${prefix(4)}::/64`, network: `${prefix(3)}::/48` };
}

/** Whether the request carries the judges' password, with wrong guesses limited per address and network, each hour. */
export async function isJudge(request: Request, env: GateEnv, store: Store, now = new Date()): Promise<boolean> {
	const given = request.headers.get("x-openport-judge");
	if (!given || !env.JUDGE_PASSWORD) return false;
	const hour = now.toISOString().slice(0, 13);
	const { address, network } = guessers(request.headers.get("cf-connecting-ip") ?? "unknown");
	const keys = [`${MISSES}${hour}:address:${address}`, `${MISSES}${hour}:network:${network}`];
	const [byAddress, byNetwork] = await Promise.all(keys.map(async (key) => (await store.get<number>(key)) ?? 0));
	if (byAddress >= MISSES_PER_HOUR || byNetwork >= NETWORK_MISSES_PER_HOUR) return false;
	if (await same(given, env.JUDGE_PASSWORD)) return true;
	await Promise.all([store.put(keys[0], byAddress + 1), store.put(keys[1], byNetwork + 1)]);
	// Earlier hours' counts are done with: dropped here, so they don't pile up.
	const stale = await store.list<number>({ prefix: MISSES, end: `${MISSES}${hour}`, limit: 128 });
	if (stale.size) await store.delete([...stale.keys()]);
	return false;
}

/** What a request that reached the container was allowed: the header the server reads, and, once it has answered,
 * whether the day's slot it took is kept. */
export interface Admission {
	reuseOnly: "judges" | "1" | null;
	settle(response: Response | null): Promise<void>;
}

/** The server caps a day's spend, but it forgets when the container sleeps. The Durable Object's storage doesn't: it
 * counts the runs started, and the pull requests opened, each UTC day, and past the cap the server may only show what
 * it already has. With a judges' password set, any request that can do work needs someone who gives it: the check
 * fails closed, whatever the path, rather than trusting it to match. */
export async function admit(request: Request, env: GateEnv, store: Store, now = new Date()): Promise<Admission> {
	const path = new URL(request.url).pathname;
	const starting = request.method === "POST" && RUN_STARTS.has(path);
	const opening = request.method === "POST" && PR_OPENS.test(path);
	const date = now.toISOString().slice(0, 10);
	const counter = starting ? `new-runs:${date}` : opening ? `new-prs:${date}` : null;
	const nothing = async () => {};
	if (mutating(request) && env.JUDGE_PASSWORD && !(await isJudge(request, env, store, now))) {
		return { reuseOnly: "judges", settle: nothing };
	}
	if (!counter) return { reuseOnly: null, settle: nothing };
	const cap = Number((starting ? env.NEW_RUNS_PER_DAY : env.NEW_PRS_PER_DAY) || 20);
	const used = (await store.get<number>(counter)) ?? 0;
	if (used >= cap) return { reuseOnly: "1", settle: nothing };
	// The slot is taken before the run starts, so requests arriving together can't all pass the cap.
	await store.put(counter, used + 1);
	return {
		reuseOnly: null,
		async settle(response) {
			const answer = response?.ok
				? ((await response.clone().json().catch(() => null)) as { reused?: boolean; opened?: boolean } | null)
				: null;
			const counted = starting ? answer?.reused === false : answer?.opened === true;
			if (counted) return;
			// A replay, a pull request that was already open, an error, or no answer at all: the slot goes back.
			const current = (await store.get<number>(counter)) ?? 1;
			await store.put(counter, Math.max(0, current - 1));
		},
	};
}
