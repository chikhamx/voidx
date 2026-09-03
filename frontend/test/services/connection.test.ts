import { describe, expect, it } from "vitest";
import { withDesktopGatewayCapabilities } from "../../src/services/connection";

describe("desktop gateway capabilities", () => {
    it("adds transcript_window_v1 while preserving and deduplicating existing capabilities", () => {
        const result = withDesktopGatewayCapabilities(
            "ws://127.0.0.1:8765/rpc?cap=existing_v1&cap=existing_v1",
        );
        const capabilities = new URL(result).searchParams
            .getAll("cap")
            .flatMap((value) => value.split(","));

        expect(capabilities).toContain("transcript_window_v1");
        expect(capabilities).toContain("existing_v1");
        expect(capabilities.filter((capability) => capability === "existing_v1")).toHaveLength(1);
        expect(capabilities.filter((capability) => capability === "transcript_window_v1")).toHaveLength(1);
    });
});
