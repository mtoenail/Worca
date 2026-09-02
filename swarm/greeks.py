# greeks.py  — local Black-Scholes fallback for when the feed returns None
import numpy as np
from scipy.stats import norm
from scipy.optimize import brentq

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
