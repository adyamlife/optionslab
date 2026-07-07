import numpy as np
from scipy.stats import norm


def d1(S, K, T, r, sigma):
    return (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))


def delta(S, K, T, r, sigma, option_type):
    D1 = d1(S, K, T, r, sigma)
    if option_type == "call":
        return norm.cdf(D1)
    return norm.cdf(D1) - 1  # put delta (negative)


def theta(S, K, T, r, sigma, option_type):
    """Daily theta decay in $ per share per day (negative = option loses value for long holder).
    Divide the standard annual theta by 365 to get the per-calendar-day figure."""
    if T <= 0 or sigma <= 0:
        return 0.0
    D1 = d1(S, K, T, r, sigma)
    D2 = D1 - sigma * np.sqrt(T)
    annual = -(S * norm.pdf(D1) * sigma) / (2.0 * np.sqrt(T))
    if option_type == "call":
        annual -= r * K * np.exp(-r * T) * norm.cdf(D2)
    else:
        annual += r * K * np.exp(-r * T) * norm.cdf(-D2)
    return annual / 365.0


def gamma(S, K, T, r, sigma):
    """Gamma: rate of change of delta per $1 move in the underlying (same for calls and puts)."""
    if T <= 0 or sigma <= 0:
        return 0.0
    D1 = d1(S, K, T, r, sigma)
    return norm.pdf(D1) / (S * sigma * np.sqrt(T))


def vega(S, K, T, r, sigma):
    """Vega in $ per 1% change in implied volatility (per share)."""
    if T <= 0 or sigma <= 0:
        return 0.0
    D1 = d1(S, K, T, r, sigma)
    return S * norm.pdf(D1) * np.sqrt(T) / 100.0
