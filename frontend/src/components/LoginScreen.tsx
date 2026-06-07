import { useState } from "react";
import { Brain, Loader2, Lock, KeyRound } from "lucide-react";
import { useMutation } from "@tanstack/react-query";
import { api } from "../lib/api";
import { haptic } from "../lib/native";

interface Props {
  onLoggedIn: () => void;
}

/**
 * Full-screen password gate shown until /api/auth/status returns
 * `authenticated: true`. The first password is the seeded "nuroq" — the
 * /api/auth/status flag `must_change_password` lets the authenticated app
 * surface a banner reminding the user to rotate it.
 */
export function LoginScreen({ onLoggedIn }: Props) {
  const [password, setPassword] = useState("");
  const [err, setErr] = useState<string | null>(null);

  const login = useMutation({
    mutationFn: (pw: string) => api.login(pw),
    onSuccess: (r) => {
      if (r.ok) {
        haptic.success();
        setErr(null);
        onLoggedIn();
      } else {
        haptic.error();
        setErr(r.detail ?? "Wrong password.");
      }
    },
    onError: (e: Error) => {
      haptic.error();
      // 401 from the wrapper or any other fetch failure.
      setErr(/401/.test(e.message) ? "Wrong password." : "Could not reach backend.");
    },
  });

  const onSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!password) return;
    setErr(null);
    login.mutate(password);
  };

  return (
    <div className="min-h-[100dvh] flex items-center justify-center bg-zinc-50 dark:bg-zinc-950 px-4">
      <form
        onSubmit={onSubmit}
        className="card w-full max-w-sm p-6 space-y-4"
      >
        <div className="flex items-center gap-2 mb-2">
          <Brain className="w-6 h-6 text-accent" />
          <div>
            <div className="font-semibold">NuroQ</div>
            <div className="text-xxs opacity-60 font-mono">Frontier Neural Quant</div>
          </div>
        </div>

        <label className="block text-xs">
          <span className="opacity-70 flex items-center gap-1.5 mb-1">
            <Lock className="w-3.5 h-3.5" /> Password
          </span>
          <input
            type="password"
            autoFocus
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="input"
            placeholder="Enter password"
            disabled={login.isPending}
          />
        </label>

        {err && (
          <div className="text-xs text-sell" role="alert">{err}</div>
        )}

        <button
          type="submit"
          disabled={login.isPending || !password}
          className="btn btn-primary w-full"
        >
          {login.isPending ? <><Loader2 className="w-3.5 h-3.5 animate-spin" /> Signing in…</> : "Sign in"}
        </button>

        <p className="text-xxs opacity-60 leading-relaxed">
          First-time login uses the seeded password{" "}
          <code className="font-mono px-1 py-0.5 rounded bg-zinc-100 dark:bg-zinc-900">nuroq</code>.
          You'll be prompted to change it after signing in.
        </p>
      </form>
    </div>
  );
}

/**
 * Inline password-change panel surfaced when the seeded password is still
 * active. Lives outside the LoginScreen so it shows AFTER login as a banner.
 */
export function ChangePasswordPanel({ onDone }: { onDone: () => void }) {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [err, setErr] = useState<string | null>(null);

  const change = useMutation({
    mutationFn: () => api.changePassword(current, next),
    onSuccess: (r) => {
      if (r.ok) { haptic.success(); onDone(); }
      else { haptic.error(); setErr(r.detail ?? "Could not change password."); }
    },
    onError: (e: Error) => {
      haptic.error();
      setErr(/401/.test(e.message) ? "Current password is wrong." : "Request failed.");
    },
  });

  const onSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    setErr(null);
    if (next.length < 6) return setErr("New password must be at least 6 characters.");
    if (next !== confirm) return setErr("Passwords don't match.");
    change.mutate();
  };

  return (
    <form onSubmit={onSubmit} className="card p-4 space-y-3 max-w-md">
      <div className="flex items-center gap-2">
        <KeyRound className="w-4 h-4 text-accent" />
        <div className="font-semibold text-sm">Change password</div>
      </div>
      <p className="text-xs opacity-70">
        You're using the seeded password. Set a strong one — 12+ chars or a passphrase.
      </p>
      <input type="password" autoComplete="current-password"
             placeholder="Current password"
             value={current} onChange={(e) => setCurrent(e.target.value)}
             className="input" />
      <input type="password" autoComplete="new-password"
             placeholder="New password"
             value={next} onChange={(e) => setNext(e.target.value)}
             className="input" />
      <input type="password" autoComplete="new-password"
             placeholder="Confirm new password"
             value={confirm} onChange={(e) => setConfirm(e.target.value)}
             className="input" />
      {err && <div className="text-xs text-sell" role="alert">{err}</div>}
      <button type="submit" disabled={change.isPending || !current || !next}
              className="btn btn-primary w-full">
        {change.isPending ? <><Loader2 className="w-3.5 h-3.5 animate-spin" /> Saving…</> : "Save new password"}
      </button>
    </form>
  );
}
