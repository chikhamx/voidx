export interface CanonicalRenderRequest {
  type: "render";
  jobId: number;
  itemId: string;
  revision: number;
  generation: number;
  canonicalText: string;
}

interface CanonicalSourceBinding {
    /** UTF-16 code unit offsets suitable for canonicalText.slice(). */
    sourceStart: number;
    sourceEnd: number;
    /** SHA-256 of the source slice's UTF-8 bytes, lowercase hexadecimal. */
    sourceHash: string;
}

export type CanonicalBlockDescriptor = CanonicalSourceBinding & (
    | {
        kind: "html";
        html: string;
        sourceLength: number;
    }
    | {
        kind: "text";
        text: string;
        reason: "html_block_budget";
    }
);

interface CanonicalRenderIdentity {
  jobId: number;
  itemId: string;
  revision: number;
  generation: number;
}

export interface CanonicalRenderSuccess extends CanonicalRenderIdentity {
  type: "rendered";
  blocks: CanonicalBlockDescriptor[];
}

export interface CanonicalRenderFailure extends CanonicalRenderIdentity {
  type: "failed";
  reason: "worker_parse" | "worker_protocol";
}

export type CanonicalRenderResponse =
  | CanonicalRenderSuccess
  | CanonicalRenderFailure;
