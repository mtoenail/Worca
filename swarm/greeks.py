# greeks.py  — local Black-Scholes fallback for when the feed returns None
import math
import numpy as np

try:
    from scipy.optimize import brentq
except Exception:
    def brentq(f, a, b, xtol=1e-8, maxiter=100):
        fa, fb = f(a), f(b)
        if fa * fb > 0:
            raise ValueError("Root is not bracketed")
        for _ in range(maxiter):
            mid = 0.5 * (a + b)
            if abs(b - a) < xtol:
                return mid
            fmid = f(mid)
            if abs(fmid) < xtol or fmid == 0:
                return mid
            if fa * fmid < 0:
                b, fb = mid, fmid
            else:
                a, fa = mid, fmid
        return 0.5 * (a + b)

_INV_SQRT_2PI = 1.0 / math.sqrt(2.0 * math.pi)
_SQRT_2 = math.sqrt(2.0)

class _Norm:
    """Lightweight normal distribution replacing scipy.stats.norm.
    Avoids Windows Application Control / WDAC policy blocking on scipy C-extension DLLs
    and provides significantly faster scalar and vectorized evaluations.
    """
    @staticmethod
    def pdf(x):
        return _INV_SQRT_2PI * np.exp(-0.5 * (np.asarray(x, dtype=float) ** 2))

    @staticmethod
    def cdf(x):
        if isinstance(x, np.ndarray):
            return 0.5 * (1.0 + np.vectorize(math.erf)(x / _SQRT_2))
        return 0.5 * (1.0 + math.erf(float(x) / _SQRT_2))

norm = _Norm()

def bs_price(S, K, T, r, sigma, call=True):
    if T <= 0 or sigma <= 0: return max(0.0, (S-K) if call else (K-S))
    d1 = (np.log(S/K) + (r + sigma**2/2)*T) / (sigma*np.sqrt(T))
    d2 = d1 - sigma*np.sqrt(T)
    if call: return S*norm.cdf(d1) - K*np.exp(-r*T)*norm.cdf(d2)
    return K*np.exp(-r*T)*norm.cdf(-d2) - S*norm.cdf(-d1)

def implied_vol(mid, S, K, T, r, call=True):
    if mid <= 0 or T <= 0: return None
    try:
        return brentq(lambda s: bs_price(S, K, T, r, s, call) - mid, 1e-4, 5.0, maxiter=100)
    except Exception:
        return None                      # no root -> skip this contract

def bs_gamma(S, K, T, r, sigma):
    if T <= 0 or sigma <= 0: return 0.0
    d1 = (np.log(S/K) + (r + sigma**2/2)*T) / (sigma*np.sqrt(T))
    return norm.pdf(d1) / (S*sigma*np.sqrt(T))

def bs_gamma_vec(S, K, T, r, sigma):
    """Vectorised gamma for a whole book at one hypothetical spot `S`.

    Used by the gamma flip scan, which re-prices every contract across a grid of spot
    levels; the scalar bs_gamma would make that ~80k Python calls per cycle.
    """
    K, T, sigma = np.asarray(K, float), np.asarray(T, float), np.asarray(sigma, float)
    out = np.zeros(K.shape, dtype=float)
    m = (T > 0) & (sigma > 0) & (K > 0)
    if not m.any():
        return out
    d1 = (np.log(S / K[m]) + (r + sigma[m] ** 2 / 2) * T[m]) / (sigma[m] * np.sqrt(T[m]))
    out[m] = norm.pdf(d1) / (S * sigma[m] * np.sqrt(T[m]))
    return out

def bs_delta(S, K, T, r, sigma, call=True):
    """Delta, for strike selection when the feed omits greeks.

    The executor picks the ~0.35-delta strike, so a missing delta on the one contract
    we intended to trade would otherwise silently shift the trade to a different strike.
    """
    if T <= 0 or sigma <= 0:
        return (1.0 if S > K else 0.0) if call else (-1.0 if S < K else 0.0)
    d1 = (np.log(S/K) + (r + sigma**2/2)*T) / (sigma*np.sqrt(T))
    return float(norm.cdf(d1)) if call else float(norm.cdf(d1) - 1.0)
