import { Container, getContainer } from "@cloudflare/containers";

interface Env {
	DASHBOARD: DurableObjectNamespace<Dashboard>;
	NEBIUS_API_KEY: string;
	TAVILY_API_KEY: string;
	OPENPORT_DAILY_BUDGET_USD: string;
}

/** OpenPort's dashboard (`openport serve`), from the repo's Dockerfile. */
export class Dashboard extends Container<Env> {
	defaultPort = 8000;
	sleepAfter = "15m";
	// Worker secrets reach the container as environment variables when it starts. One that isn't set is left out:
	// with no keys the dashboard still replays its recorded runs.
	envVars = Object.fromEntries(
		Object.entries({
			NEBIUS_API_KEY: this.env.NEBIUS_API_KEY,
			TAVILY_API_KEY: this.env.TAVILY_API_KEY,
			OPENPORT_DAILY_BUDGET_USD: this.env.OPENPORT_DAILY_BUDGET_USD,
		}).filter(([, value]) => value),
	);
}

export default {
	async fetch(request: Request, env: Env): Promise<Response> {
		// Every visitor reaches the same instance: a run started by one request is streamed to the next.
		return getContainer(env.DASHBOARD, "dashboard").fetch(request);
	},
} satisfies ExportedHandler<Env>;
