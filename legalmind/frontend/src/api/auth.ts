import { api } from "./client";
import type { CurrentUser, MfaEnrollment } from "./types";

export function login(username: string, password: string) {
  return api<CurrentUser>("/auth/login", {
    method: "POST",
    body: JSON.stringify({ username, password }),
  });
}

export function logout() {
  return api<void>("/auth/logout", { method: "POST" });
}

export function me() {
  return api<CurrentUser>("/auth/me");
}

export function startMfaEnrollment() {
  return api<MfaEnrollment>("/auth/mfa/enroll", { method: "POST" });
}

export function confirmMfaEnrollment(code: string) {
  return api<{ recovery_codes: string[] }>("/auth/mfa/confirm", {
    method: "POST",
    body: JSON.stringify({ code }),
  });
}

export function verifyMfa(code: string) {
  return api<void>("/auth/mfa/verify", { method: "POST", body: JSON.stringify({ code }) });
}
