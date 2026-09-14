// Types for the `env` and `exports` that `cloudflare:workers` hands the integration tests.
//
// `Cloudflare.Env` is an interface, so it merges by redeclaration. `Cloudflare.Exports` is a type
// *derived* from `Cloudflare.GlobalProps["mainModule"]`, so it is populated by declaring the main
// module here instead — which is what makes `exports.default.fetch()` typed. Doing it by hand keeps
// the tests independent of a generated worker-configuration.d.ts.
import type { MailWorkerEnv } from "../../src/env";

declare global {
	namespace Cloudflare {
		interface Env extends MailWorkerEnv {}

		interface GlobalProps {
			mainModule: typeof import("../../src/index");
			durableNamespaces: "InboxQueue";
		}
	}
}

export {};
