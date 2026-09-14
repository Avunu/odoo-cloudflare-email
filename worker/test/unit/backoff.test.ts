import { describe, it, expect } from "vitest";
import {
	DEFAULT_BACKOFF_SECONDS,
	backoffMs,
	classifyStatus,
	parseSchedule,
} from "../../src/lib/backoff";
import type { Outcome } from "../../src/lib/backoff";

describe("DEFAULT_BACKOFF_SECONDS", () => {
	it("climbs from a minute to six hours", () => {
		expect(DEFAULT_BACKOFF_SECONDS).toEqual([60, 300, 900, 3600, 21_600]);
	});
});

describe("parseSchedule", () => {
	it("parses a comma-separated list, tolerating whitespace", () => {
		expect(parseSchedule("60,300,900")).toEqual([60, 300, 900]);
		expect(parseSchedule(" 60 , 300 ")).toEqual([60, 300]);
		expect(parseSchedule("5")).toEqual([5]);
	});

	it("rejects anything that is not a positive integer", () => {
		for (const raw of ["", "60,", "60,,300", "60,abc", "3OO", "-5", "1.5", "0", "60,0", "1e3"]) {
			expect(() => parseSchedule(raw), raw).toThrow();
		}
	});
});

describe("backoffMs", () => {
	const schedule = [60, 300];

	it("walks the schedule by attempt number and returns milliseconds", () => {
		expect(backoffMs(schedule, 1)).toBe(60_000);
		expect(backoffMs(schedule, 2)).toBe(300_000);
	});

	it("repeats the last value past the end of the schedule", () => {
		expect(backoffMs(schedule, 3)).toBe(300_000);
		expect(backoffMs(schedule, 50)).toBe(300_000);
		expect(backoffMs(DEFAULT_BACKOFF_SECONDS, 32)).toBe(21_600_000);
	});

	it("treats an attempt below 1 as the first", () => {
		expect(backoffMs(schedule, 0)).toBe(60_000);
	});

	it("refuses an empty schedule", () => {
		expect(() => backoffMs([], 1)).toThrow(RangeError);
	});
});

describe("classifyStatus", () => {
	const table: [number, Outcome][] = [
		[200, "delivered"],
		[201, "delivered"],
		[204, "delivered"],
		[299, "delivered"],
		[408, "retry"],
		[429, "retry"],
		[500, "retry"],
		[502, "retry"],
		[503, "retry"],
		[504, "retry"],
		[599, "retry"],
		[100, "rejected"],
		[301, "rejected"],
		[302, "rejected"],
		[307, "rejected"],
		[308, "rejected"],
		[400, "rejected"],
		[401, "rejected"],
		[403, "rejected"],
		[404, "rejected"],
		[405, "rejected"],
		[410, "rejected"],
		[413, "rejected"],
		[422, "rejected"],
		[600, "rejected"],
	];

	it.each(table)("%i → %s", (status, outcome) => {
		expect(classifyStatus(status)).toBe(outcome);
	});
});
