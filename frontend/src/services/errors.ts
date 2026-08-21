import axios from "axios";

// Turn any thrown error (axios / network / unknown) into a single, friendly, client-safe sentence.
// The backend already returns client-safe `detail` strings (no stack traces — see backend errors.py),
// so we surface those for 4xx; 5xx/unknown get a generic line. A raw traceback or SDK message must never
// reach the UI, so we defend against oversized/multi-line payloads and always fall back to a generic.

const GENERIC = "Something went wrong. Please try again.";

function looksClientSafe(detail: unknown): detail is string {
  return (
    typeof detail === "string" &&
    detail.trim().length > 0 &&
    detail.length < 300 &&
    !detail.includes("\n") &&        // multi-line → likely a traceback
    !/Traceback|File \"|line \d+/.test(detail)
  );
}

export function errorMessage(err: unknown): string {
  if (axios.isAxiosError(err)) {
    const status = err.response?.status;
    const data = err.response?.data as { detail?: unknown; request_id?: string } | undefined;
    const ref = data?.request_id ? ` (Reference: ${data.request_id})` : "";

    // No response at all → the request never reached the service.
    if (!err.response) {
      return "We couldn't reach the service. Check your connection and try again.";
    }
    switch (status) {
      case 401:
        return "Your session has expired. Please sign in again.";
      case 403:
        // 403s from us carry a client-safe reason (e.g. which subscription lacks Reader access).
        return looksClientSafe(data?.detail)
          ? (data!.detail as string)
          : "You don't have access to that resource. Check your Azure permissions and try again.";
      case 404:
        return "We couldn't find what you were looking for. It may have been removed.";
      case 429:
        return "You're going a bit fast. Please wait a moment and try again.";
      case 400:
        return looksClientSafe(data?.detail) ? (data!.detail as string) : "That request wasn't valid.";
      default:
        if (status && status >= 500) {
          return `The service is temporarily unavailable. Please try again in a moment.${ref}`;
        }
        return looksClientSafe(data?.detail) ? (data!.detail as string) : GENERIC;
    }
  }
  return GENERIC;
}
