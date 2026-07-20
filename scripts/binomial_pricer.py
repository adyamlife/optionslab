"""
Binomial Option Pricer — CRR (Cox-Ross-Rubinstein) tree with American early exercise.

Used for individual stock options where Black-Scholes misprices deep ITM puts because
it cannot guarantee price >= intrinsic value (the American put floor).

Dividend handling
-----------------
Continuous dividend yield (q) is supported via the `dividend_yield` parameter.
The risk-neutral drift becomes (r - q) instead of r, so the risk-neutral
probability is:
    p = (exp((r - q) * dt) - d) / (u - d)

Discrete dividends (known cash amounts on specific dates) are not yet supported.
When precision near ex-dividend dates is critical, pass the implied forward price
as S (i.e. S - PV(dividend)) and set dividend_yield=0.

Why CRR over Black-Scholes for American options
------------------------------------------------
  - BS deep-ITM put price can fall below intrinsic value (K - S).
  - CRR enforces max(continuation, intrinsic) at every node (Bellman recursion),
    guaranteeing price >= intrinsic at all times.
  - The early-exercise premium is priced correctly by the backward induction,
    not by any heuristic.

European index options
----------------------
SPX and XSP are cash-settled European-style — Black-Scholes is correct for these.
SPY, QQQ, and all single-stock options are American-style — use this pricer.

API
---
  price(S, K, T, r, sigma, option_type, american=True,
        dividend_yield=0.0, steps=500) -> float
  delta(...)  -> float
  gamma(...)  -> float
  theta(...)  -> float  (daily, note: finite-difference noise across tree rebuilds)
  vega(...)   -> float  (per 1% move in vol)
  rho(...)    -> float  (per 1% move in rate)
  should_use_binomial(option_type, days_to_expiry, days_to_ex_div,
                      is_european_index=False) -> bool
  price_spread(...)  -> dict

Standalone:
  python -m scripts.binomial_pricer --S 150 --K 155 --T 0.05 --sigma 0.30 --type put
"""
import argparse
import math


# ── Core tree ─────────────────────────────────────────────────────────────────

def price(
    S:               float,
    K:               float,
    T:               float,
    r:               float,
    sigma:           float,
    option_type:     str,
    american:        bool  = True,
    dividend_yield:  float = 0.0,
    steps:           int   = 500,
) -> float:
    """
    CRR binomial tree option price.

    Args:
        S:               Current underlying spot price
        K:               Strike price
        T:               Time to expiry in years
        r:               Risk-free rate (annualized, e.g. 0.05)
        sigma:           Implied volatility (annualized, e.g. 0.25)
        option_type:     "call" or "put"
        american:        Enable early-exercise (Bellman recursion) at each node
        dividend_yield:  Continuous dividend yield q (annualized, e.g. 0.02).
                         Adjusts risk-neutral drift to (r - q). For discrete
                         dividends, pass the ex-dividend-adjusted forward as S.
        steps:           Number of time steps. Higher = more accurate but slower.
                         500 is a reasonable production default for American options.

    Returns float option price per share.

    Raises:
        ValueError: if the risk-neutral probability falls outside [0, 1], which
                    indicates an arbitrage-inconsistent parameter combination
                    (e.g. very high r, very low sigma, or very few steps).
    """
    if T <= 0 or sigma <= 0:
        intrinsic = max(S - K, 0) if option_type == "call" else max(K - S, 0)
        return float(intrinsic)

    dt   = T / steps
    u    = math.exp(sigma * math.sqrt(dt))
    d    = 1.0 / u
    disc = math.exp(-r * dt)

    # Risk-neutral probability under continuous dividend yield
    p = (math.exp((r - dividend_yield) * dt) - d) / (u - d)
    if not (0.0 <= p <= 1.0):
        raise ValueError(
            f"Risk-neutral probability p={p:.6f} is outside [0, 1]. "
            f"Parameters are arbitrage-inconsistent: "
            f"r={r}, q={dividend_yield}, sigma={sigma}, dt={dt:.6f}. "
            f"Try increasing steps or checking r/sigma inputs."
        )
    q = 1.0 - p

    # Terminal node stock prices and payoffs
    if option_type == "call":
        values = [max(S * (u ** (steps - 2 * j)) - K, 0.0) for j in range(steps + 1)]
    else:
        values = [max(K - S * (u ** (steps - 2 * j)), 0.0) for j in range(steps + 1)]

    # Backward induction — Bellman recursion enforces American floor
    for i in range(steps - 1, -1, -1):
        new_values = []
        for j in range(i + 1):
            continuation = disc * (p * values[j] + q * values[j + 1])
            if american:
                node_S    = S * (u ** (i - 2 * j))
                intrinsic = max(node_S - K, 0.0) if option_type == "call" else max(K - node_S, 0.0)
                new_values.append(max(continuation, intrinsic))
            else:
                new_values.append(continuation)
        values = new_values

    return float(values[0])


# ── Greeks ────────────────────────────────────────────────────────────────────

def _bump(S: float) -> float:
    """Adaptive bump size for finite-difference Greeks. ~0.1% of spot, min $0.01."""
    return max(0.01, S * 0.001)


def delta(
    S:               float,
    K:               float,
    T:               float,
    r:               float,
    sigma:           float,
    option_type:     str,
    american:        bool  = True,
    dividend_yield:  float = 0.0,
    steps:           int   = 500,
) -> float:
    """Binomial delta via central finite difference on S."""
    h = _bump(S)
    p_up   = price(S + h, K, T, r, sigma, option_type, american, dividend_yield, steps)
    p_down = price(S - h, K, T, r, sigma, option_type, american, dividend_yield, steps)
    return round((p_up - p_down) / (2 * h), 6)


def gamma(
    S:               float,
    K:               float,
    T:               float,
    r:               float,
    sigma:           float,
    option_type:     str,
    american:        bool  = True,
    dividend_yield:  float = 0.0,
    steps:           int   = 500,
) -> float:
    """Binomial gamma via second-order central finite difference on S."""
    h     = _bump(S)
    p_up  = price(S + h, K, T, r, sigma, option_type, american, dividend_yield, steps)
    p_mid = price(S,     K, T, r, sigma, option_type, american, dividend_yield, steps)
    p_dn  = price(S - h, K, T, r, sigma, option_type, american, dividend_yield, steps)
    return round((p_up - 2 * p_mid + p_dn) / (h ** 2), 6)


def theta(
    S:               float,
    K:               float,
    T:               float,
    r:               float,
    sigma:           float,
    option_type:     str,
    american:        bool  = True,
    dividend_yield:  float = 0.0,
    steps:           int   = 500,
) -> float:
    """
    Daily theta via finite difference on T (price tomorrow minus price today).
    Returns a negative value for long positions (time decay).

    Note: rebuilding two full trees with slightly different dt introduces
    finite-difference noise proportional to 1/steps. Increase steps for
    smoother theta estimates near expiry.
    """
    if T <= 1 / 365:
        return 0.0
    p_now      = price(S, K, T,            r, sigma, option_type, american, dividend_yield, steps)
    p_tomorrow = price(S, K, T - 1 / 365, r, sigma, option_type, american, dividend_yield, steps)
    return round(p_tomorrow - p_now, 6)


def vega(
    S:               float,
    K:               float,
    T:               float,
    r:               float,
    sigma:           float,
    option_type:     str,
    american:        bool  = True,
    dividend_yield:  float = 0.0,
    steps:           int   = 500,
) -> float:
    """Vega per 1 percentage-point increase in implied vol (e.g. 25% -> 26%)."""
    bump   = 0.01
    p_up   = price(S, K, T, r, sigma + bump, option_type, american, dividend_yield, steps)
    p_down = price(S, K, T, r, sigma - bump, option_type, american, dividend_yield, steps)
    return round((p_up - p_down) / 2.0, 6)


def rho(
    S:               float,
    K:               float,
    T:               float,
    r:               float,
    sigma:           float,
    option_type:     str,
    american:        bool  = True,
    dividend_yield:  float = 0.0,
    steps:           int   = 500,
) -> float:
    """Rho per 1 percentage-point increase in risk-free rate (e.g. 5% -> 6%)."""
    bump   = 0.01
    p_up   = price(S, K, T, r + bump, sigma, option_type, american, dividend_yield, steps)
    p_down = price(S, K, T, r - bump, sigma, option_type, american, dividend_yield, steps)
    return round((p_up - p_down) / 2.0, 6)


# ── Routing helper ────────────────────────────────────────────────────────────

def should_use_binomial(
    option_type:        str,
    days_to_expiry:     int | None,
    days_to_ex_div:     int | None,
    is_european_index:  bool = False,
) -> bool:
    """
    True when the binomial pricer should replace Black-Scholes.

    European-style index options (SPX, XSP) do not have early exercise, so
    Black-Scholes is exact for them — pass is_european_index=True.
    SPY, QQQ, and all single-stock options are American-style.

    American early exercise is material for:
      - Puts any time (deep ITM put floor: price must be >= K - S)
      - Calls within 10 days of an ex-dividend date (rational to exercise early
        to capture the dividend, IF dividend_yield is modelled — see price())

    Args:
        option_type:       "call" or "put"
        days_to_expiry:    Calendar days until expiry
        days_to_ex_div:    Calendar days until next ex-dividend date (None = unknown)
        is_european_index: True for SPX/XSP cash-settled European index options.
                           False (default) for stocks, ETFs (SPY, QQQ, IWM, etc.).
    """
    if is_european_index:
        return False
    if days_to_expiry is None or days_to_expiry <= 0:
        return False
    if option_type == "put":
        return True
    if option_type == "call" and days_to_ex_div is not None:
        return 0 <= days_to_ex_div <= 10
    return False


# ── Spread pricing ────────────────────────────────────────────────────────────

def price_spread(
    S:            float,
    long_strike:  float,
    short_strike: float,
    T:            float,
    r:            float,
    long_sigma:   float,
    short_sigma:  float,
    option_type:  str,
    american:     bool  = True,
    dividend_yield: float = 0.0,
    steps:        int   = 500,
) -> dict:
    """
    Price a vertical spread (two legs) using binomial trees.

    Convention (validated by this function):
      call debit spread:   long_strike < short_strike  (buy lower, sell higher)
      put debit spread:    long_strike > short_strike  (buy higher, sell lower)
      put credit spread:   long_strike < short_strike  (sell higher, buy lower —
                           pass short as long_strike for the credit leg)

    Returns dict with: long_price, short_price, net_debit or net_credit,
                       max_profit, max_loss.
    """
    if option_type == "call":
        if long_strike >= short_strike:
            raise ValueError(
                f"Call debit spread requires long_strike < short_strike, "
                f"got long={long_strike}, short={short_strike}."
            )
    elif option_type == "put":
        if long_strike <= short_strike:
            raise ValueError(
                f"Put debit spread requires long_strike > short_strike, "
                f"got long={long_strike}, short={short_strike}. "
                f"For a put credit spread, pass the short (higher) strike as long_strike."
            )
    else:
        raise ValueError(f"option_type must be 'call' or 'put', got {option_type!r}")

    long_p  = price(S, long_strike,  T, r, long_sigma,  option_type, american, dividend_yield, steps)
    short_p = price(S, short_strike, T, r, short_sigma, option_type, american, dividend_yield, steps)

    if option_type == "call":
        net   = long_p - short_p          # positive = net debit
        width = short_strike - long_strike
        return {
            "long_price":  long_p,
            "short_price": short_p,
            "net_debit":   round(max(net, 0), 4),
            "max_profit":  round(width - max(net, 0), 4),
            "max_loss":    round(max(net, 0), 4),
        }
    else:
        # Put debit spread: long higher-strike put, short lower-strike put
        net   = long_p - short_p          # positive = net debit
        width = long_strike - short_strike
        return {
            "long_price":  long_p,
            "short_price": short_p,
            "net_debit":   round(max(net, 0), 4),
            "max_profit":  round(width - max(net, 0), 4),
            "max_loss":    round(max(net, 0), 4),
        }


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="CRR binomial option pricer")
    parser.add_argument("--S",     type=float, required=True,  help="Spot price")
    parser.add_argument("--K",     type=float, required=True,  help="Strike price")
    parser.add_argument("--T",     type=float, required=True,  help="Years to expiry")
    parser.add_argument("--r",     type=float, default=0.05,   help="Risk-free rate (default 0.05)")
    parser.add_argument("--q",     type=float, default=0.0,    help="Continuous dividend yield (default 0.0)")
    parser.add_argument("--sigma", type=float, required=True,  help="Implied vol (e.g. 0.30)")
    parser.add_argument("--type",  dest="option_type", default="put", choices=["call", "put"])
    parser.add_argument("--steps", type=int,   default=500,    help="Tree steps (default 500)")
    parser.add_argument("--european", action="store_true",     help="Price as European")
    args = parser.parse_args()

    american_flag = not args.european

    opt_price = price(args.S, args.K, args.T, args.r, args.sigma,
                      args.option_type, american_flag, args.q, args.steps)
    opt_delta = delta(args.S, args.K, args.T, args.r, args.sigma,
                      args.option_type, american_flag, args.q, args.steps)
    opt_gamma = gamma(args.S, args.K, args.T, args.r, args.sigma,
                      args.option_type, american_flag, args.q, args.steps)
    opt_theta = theta(args.S, args.K, args.T, args.r, args.sigma,
                      args.option_type, american_flag, args.q, args.steps)
    opt_vega  = vega( args.S, args.K, args.T, args.r, args.sigma,
                      args.option_type, american_flag, args.q, args.steps)
    opt_rho   = rho(  args.S, args.K, args.T, args.r, args.sigma,
                      args.option_type, american_flag, args.q, args.steps)

    style = "American" if american_flag else "European"
    print(f"\n=== CRR Binomial ({style} {args.option_type.upper()}) ===")
    print(f"  S={args.S}  K={args.K}  T={args.T:.4f}yr  vol={args.sigma:.2%}  r={args.r:.2%}  q={args.q:.2%}")
    print(f"  Steps: {args.steps}")
    print(f"\n  Price:  ${opt_price:.4f}")
    print(f"  Delta:  {opt_delta:+.4f}")
    print(f"  Gamma:  {opt_gamma:.6f}")
    print(f"  Theta:  ${opt_theta:.4f}/day")
    print(f"  Vega:   ${opt_vega:.4f} per 1% vol")
    print(f"  Rho:    ${opt_rho:.4f} per 1% rate")

    try:
        from scipy.stats import norm
        fwd = args.S * math.exp((args.r - args.q) * args.T)
        d1 = (math.log(fwd / args.K) + 0.5 * args.sigma ** 2 * args.T) \
             / (args.sigma * math.sqrt(args.T))
        d2 = d1 - args.sigma * math.sqrt(args.T)
        disc_K = args.K * math.exp(-args.r * args.T)
        disc_S = args.S * math.exp(-args.q  * args.T)
        if args.option_type == "call":
            bs_p = disc_S * norm.cdf(d1) - disc_K * norm.cdf(d2)
        else:
            bs_p = disc_K * norm.cdf(-d2) - disc_S * norm.cdf(-d1)
        print(f"\n  BS ref: ${bs_p:.4f}  (early-exercise premium: ${opt_price - bs_p:+.4f})")
    except Exception:
        pass
