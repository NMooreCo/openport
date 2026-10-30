import { DurableObject } from "cloudflare:workers";

interface Env {
	DASHBOARD: DurableObjectNamespace<Dashboard>;
	NEBIUS_API_KEY?: string;
	TAVILY_API_KEY?: string;
	OPENPORT_DAILY_BUDGET_USD: string;
	NEW_RUNS_PER_DAY: string;
	// Set as a secret, live runs are for whoever has it (the hackathon's judges); everyone else sees the replays.
	JUDGE_PASSWORD?: string;
	VIDEO_URL?: string;
	// A token for a GitHub organization set aside for OpenPort's pull requests (SANDBOX_ORG), as a secret: with it, a
	// finished live port's pull request can be opened there.
	GITHUB_TOKEN?: string;
	SANDBOX_ORG?: string;
	NEW_PRS_PER_DAY: string;
}

// Requests that can start a run (and spend Token Factory and Tavily credit) rather than replay one.
const RUN_STARTS = new Set(["/api/runs", "/api/upgrades"]);
// Requests that open a run's pull request on GitHub.
const PR_OPENS = /^\/api\/runs\/[a-f0-9]+\/github$/;
// Wrong passwords one address may try in an hour; past that its guesses aren't checked until the next hour.
const MISSES_PER_HOUR = 20;

const sha256 = (text: string) => crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));

/** Whether two secrets match, taking the same time whatever they hold. */
async function same(given: string, secret: string): Promise<boolean> {
	const [a, b] = await Promise.all([sha256(given), sha256(secret)]);
	return crypto.subtle.timingSafeEqual(a, b);
}

async function digest(text: string): Promise<string> {
	return [...new Uint8Array(await sha256(text))].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

// `openport serve` in the repo's Dockerfile.
const PORT = 8000;
// The container sleeps after this long without a request; the next one wakes it in a few seconds.
const IDLE_MS = 15 * 60 * 1000;

/** OpenPort's dashboard, in a container this Durable Object starts and forwards every request to. */
export class Dashboard extends DurableObject<Env> {
	private starting: Promise<void> | undefined;

	constructor(ctx: DurableObjectState, env: Env) {
		super(ctx, env);
		// The timeout isn't kept across a restart of this object, such as a deploy. (No container at all in
		// `wrangler dev --enable-containers=false`, where only /api/site answers.)
		const container = ctx.container;
		if (container?.running) {
			void ctx.blockConcurrencyWhile(() => container.setInactivityTimeout(IDLE_MS));
		}
	}

	async fetch(request: Request): Promise<Response> {
		const url = new URL(request.url);
		if (url.pathname === "/api/site") return this.site(request); // answered here: no need to wake the container

		// Anything but GET and HEAD can do work: start a run, open a pull request.
		const mutating = request.method !== "GET" && request.method !== "HEAD";
		// The server routes the decoded path, so /api%2Fruns or /api//runs would reach a run start without matching the
		// checks below. The dashboard never sends either.
		if (mutating && /%|\/\//.test(url.pathname)) return Response.json({ detail: "Not Found" }, { status: 404 });

		// Requests that arrive while it starts share one start and one readiness check.
		this.starting ??= this.start().finally(() => {
			this.starting = undefined;
		});
		await this.starting;

		url.protocol = "http:";
		url.host = "container";
		const forwarded = new Request(url, request);
		forwarded.headers.delete("host");
		forwarded.headers.delete("x-openport-judge"); // the password stops here
		forwarded.headers.delete("x-openport-reuse-only"); // only this object says what a request may do

		// The server caps a day's spend, but it forgets when the container sleeps. This object's storage doesn't:
		// it counts the runs started, and the pull requests opened, each UTC day, and past the cap the server may only
		// show what it already has. With a judges' password set, any request that can do work needs someone who gives
		// it: the check fails closed, whatever the path, rather than trusting it to match.
		const starting = request.method === "POST" && RUN_STARTS.has(url.pathname);
		const opening = request.method === "POST" && PR_OPENS.test(url.pathname);
		const date = new Date().toISOString().slice(0, 10);
		const counter = starting ? `new-runs:${date}` : opening ? `new-prs:${date}` : null;
		const cap = Number((starting ? this.env.NEW_RUNS_PER_DAY : this.env.NEW_PRS_PER_DAY) || 20);
		let reserved = false;
		if (mutating && this.env.JUDGE_PASSWORD && !(await this.isJudge(request))) {
			forwarded.headers.set("x-openport-reuse-only", "judges");
		} else if (counter) {
			const used = (await this.ctx.storage.get<number>(counter)) ?? 0;
			if (used >= cap) {
				forwarded.headers.set("x-openport-reuse-only", "1");
			} else {
				// The slot is taken before the run starts, so requests arriving together can't all pass the cap.
				await this.ctx.storage.put(counter, used + 1);
				reserved = true;
			}
		}
		const response = await this.ctx.container!.getTcpPort(PORT).fetch(forwarded);
		if (reserved && counter) {
			const answer = response.ok
				? ((await response.clone().json().catch(() => null)) as { reused?: boolean; opened?: boolean } | null)
				: null;
			const counted = starting ? answer?.reused === false : answer?.opened === true;
			if (!counted) {
				// A replay, a pull request that was already open, or an error: the slot goes back.
				const now = (await this.ctx.storage.get<number>(counter)) ?? 1;
				await this.ctx.storage.put(counter, Math.max(0, now - 1));
			}
		}
		return response;
	}

	/** The page's /api/site: whether live runs need the password, whether this visitor gave it, the video, and the
	 * GitHub organization pull requests open in. */
	private async site(request: Request): Promise<Response> {
		const gated = Boolean(this.env.JUDGE_PASSWORD);
		const body = {
			gated,
			judge: gated ? await this.isJudge(request) : true,
			video_url: this.env.VIDEO_URL || null,
			github_org: this.env.GITHUB_TOKEN && this.env.SANDBOX_ORG ? this.env.SANDBOX_ORG : null,
		};
		return Response.json(body, { headers: { "cache-control": "no-store", "x-content-type-options": "nosniff" } });
	}

	/** Whether the request carries the judges' password, with wrong guesses limited per address and hour. */
	private async isJudge(request: Request): Promise<boolean> {
		const given = request.headers.get("x-openport-judge");
		if (!given || !this.env.JUDGE_PASSWORD) return false;
		const address = request.headers.get("cf-connecting-ip") ?? "unknown";
		const key = `judge-misses:${new Date().toISOString().slice(0, 13)}:${address}`;
		const misses = (await this.ctx.storage.get<number>(key)) ?? 0;
		if (misses >= MISSES_PER_HOUR) return false;
		if (await same(given, this.env.JUDGE_PASSWORD)) return true;
		await this.ctx.storage.put(key, misses + 1);
		return false;
	}

	private async start(): Promise<void> {
		const container = this.ctx.container!;
		const image = container.images.dashboard;
		// Worker secrets reach the container as environment variables. One that isn't set is left out: with no
		// keys the dashboard still replays its recorded runs.
		const env = Object.fromEntries(
			Object.entries({
				NEBIUS_API_KEY: this.env.NEBIUS_API_KEY,
				TAVILY_API_KEY: this.env.TAVILY_API_KEY,
				OPENPORT_DAILY_BUDGET_USD: this.env.OPENPORT_DAILY_BUDGET_USD,
				OPENPORT_GITHUB_TOKEN: this.env.GITHUB_TOKEN,
				OPENPORT_SANDBOX_ORG: this.env.SANDBOX_ORG,
			}).filter((entry): entry is [string, string] => Boolean(entry[1])),
		);
		// What the running container was started with, as a digest (no secret is stored). A deploy with a new image,
		// or a new key or budget, replaces it now rather than when it next idles out.
		const wanted = await digest(JSON.stringify([image, env]));
		if (container.running && (await this.ctx.storage.get<string>("started-with")) !== wanted) {
			await container.destroy("its image or settings changed");
		}
		if (!container.running) {
			await this.ctx.storage.put("started-with", wanted);
			container.start({
				image,
				// 1/2 vCPU, 4 GiB, 8 GB disk.
				instance: "standard-1",
				// Live runs call Token Factory, Tavily and GitHub.
				enableInternet: true,
				env,
			});
		}
		await container.setInactivityTimeout(IDLE_MS);

		// start() returns before the server inside is up: wait until it answers.
		const port = container.getTcpPort(PORT);
		let lastError: unknown;
		for (let attempt = 0; attempt < 150; attempt++) {
			try {
				const response = await port.fetch("http://container/api/info", { signal: AbortSignal.timeout(1000) });
				await response.body?.cancel();
				if (response.ok) return;
				lastError = new Error(`/api/info returned ${response.status}`);
			} catch (error) {
				lastError = error;
			}
			await scheduler.wait(200);
		}
		throw new Error(`The dashboard didn't start listening on port ${PORT}`, { cause: lastError });
	}
}

export default {
	fetch(request: Request, env: Env): Promise<Response> {
		// Every visitor reaches the same instance: a run started by one request is streamed to the next.
		return env.DASHBOARD.getByName("dashboard").fetch(request);
	},
} satisfies ExportedHandler<Env>;
