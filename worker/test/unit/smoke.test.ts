import { describe, it, expect } from "vitest";
import { VERSION } from "../../src/version";

// Placeholder so the unit runner has something to run until the lib/ suites land. The assertion is
// still worth keeping afterwards: Release Please rewrites this constant, and a bad stamp would
// otherwise only surface in Odoo's logs as a garbled User-Agent.
describe("version", () => {
	it("is a plain semver, as Release Please stamps it", () => {
		expect(VERSION).toMatch(/^\d+\.\d+\.\d+$/);
	});
});
