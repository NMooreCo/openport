import { DurableObject } from "cloudflare:workers";

interface Env {
	DASHBOARD: DurableObjectNamespace<Dashboard>;
	NEBIUS_API_KEY?: string;
	TAVILY_API_KEY?: string;
	OPENPORT_DAILY_BUDGET_USD: string;
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
		// The timeout isn't kept across a restart of this object, such as a deploy.
		const container = ctx.container!;
		if (container.running) {
			void ctx.blockConcurrencyWhile(() => container.setInactivityTimeout(IDLE_MS));
		}
	}

	async fetch(request: Request): Promise<Response> {
		// Requests that arrive while it starts share one start and one readiness check.
		this.starting ??= this.start().finally(() => {
			this.starting = undefined;
		});
		await this.starting;

		const url = new URL(request.url);
		url.protocol = "http:";
		url.host = "container";
		const forwarded = new Request(url, request);
		forwarded.headers.delete("host");
		return this.ctx.container!.getTcpPort(PORT).fetch(forwarded);
	}

	private async start(): Promise<void> {
		const container = this.ctx.container!;
		if (!container.running) {
			container.start({
				image: container.images.dashboard,
				// 1/2 vCPU, 4 GiB, 8 GB disk.
				instance: "standard-1",
				// Live runs call Token Factory, Tavily and GitHub.
				enableInternet: true,
				// Worker secrets reach the container as environment variables. One that isn't set is left out: with no
				// keys the dashboard still replays its recorded runs.
				env: Object.fromEntries(
					Object.entries({
						NEBIUS_API_KEY: this.env.NEBIUS_API_KEY,
						TAVILY_API_KEY: this.env.TAVILY_API_KEY,
						OPENPORT_DAILY_BUDGET_USD: this.env.OPENPORT_DAILY_BUDGET_USD,
					}).filter((entry): entry is [string, string] => Boolean(entry[1])),
				),
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
