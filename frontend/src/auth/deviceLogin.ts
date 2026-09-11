import axios from "axios";
import { Session } from "./AuthProvider";

/** Pre-auth calls to the backend device-code endpoints (no bearer token yet). */
const http = axios.create({ baseURL: "/api" });

export interface DeviceStart {
  session_id: string;
  user_code: string;
  verification_uri: string;
  expires_in: number;
  interval: number;
}

export interface DevicePoll {
  status: "pending" | "complete";
  access_token?: string;
  expires_on?: number;
  account?: Session["account"];
}

export class DeviceLoginError extends Error {
  /** true when the sign-in must be restarted (code expired / declined). */
  restart: boolean;
  constructor(message: string, restart: boolean) {
    super(message);
    this.restart = restart;
  }
}

function toError(err: unknown, fallback: string): DeviceLoginError {
  if (axios.isAxiosError(err)) {
    const detail = (err.response?.data as { detail?: string } | undefined)?.detail;
    const restart = err.response?.status === 401;
    return new DeviceLoginError(detail || fallback, restart);
  }
  return new DeviceLoginError(fallback, false);
}

export async function startDeviceLogin(): Promise<DeviceStart> {
  try {
    const { data } = await http.post<DeviceStart>("/auth/device/start");
    return data;
  } catch (err) {
    throw toError(err, "Could not start Microsoft sign-in. Please try again.");
  }
}

export async function pollDeviceLogin(sessionId: string): Promise<DevicePoll> {
  try {
    const { data } = await http.post<DevicePoll>("/auth/device/poll", { session_id: sessionId });
    return data;
  } catch (err) {
    throw toError(err, "Microsoft sign-in could not be completed. Please try again.");
  }
}
