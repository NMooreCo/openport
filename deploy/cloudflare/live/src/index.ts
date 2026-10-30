import { DurableObject } from "cloudflare:workers";

import { admit, isJudge, refused, type GateEnv } from "./gate";

interface Env extends GateEnv {
	DASHBOARD: DurableObjectNamespace<Dashboard>;
	NEBIUS_API_KEY?: string;
	TAVILY_API_KEY?: string;
	OPENPORT_DAILY_BUDGET_USD: string;
	VIDEO_URL?: string;
	// A token for a GitHub organization set aside for OpenPort's pull requests (SANDBOX_ORG), as a secret: with it, a
	// finished live port's pull request can be opened there.
	GITHUB_TOKEN?: string;
	SANDBOX_ORG?: string;
}

const sha256 = (text: string) => crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));

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

		if (refused(request)) return Response.json({ detail: "Not Found" }, { status: 404 });

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

		const admission = await admit(request, this.env, this.ctx.storage);
		if (admission.reuseOnly) forwarded.headers.set("x-openport-reuse-only", admission.reuseOnly);
		let response: Response | null = null;
		try {
			response = await this.ctx.container!.getTcpPort(PORT).fetch(forwarded);
		} finally {
			await admission.settle(response); // a day's slot goes back unless the run or pull request happened
		}
		return response;
	}

	/** The page's /api/site: whether live runs need the password, whether this visitor gave it, the video, and the
	 * GitHub organization pull requests open in. */
	private async site(request: Request): Promise<Response> {
		const gated = Boolean(this.env.JUDGE_PASSWORD);
		const body = {
			gated,
			judge: gated ? await isJudge(request, this.env, this.ctx.storage) : true,
			video_url: this.env.VIDEO_URL || null,
			github_org: this.env.GITHUB_TOKEN && this.env.SANDBOX_ORG ? this.env.SANDBOX_ORG : null,
		};
		return Response.json(body, { headers: { "cache-control": "no-store", "x-content-type-options": "nosniff" } });
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
