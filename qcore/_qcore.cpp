// qcore/_qcore.cpp — C++ compute kernel for the quant scanner (Phase 2a).
//
// Black-Scholes price/gamma/vega + bisection implied-vol, matching
// quant/mathutils.py bit-for-bit: same guards, same RISK_FREE, same 80-iter
// bisection with the 1e-5 width stop and no-arbitrage rejection. Built as the
// pybind11 extension `qcore._qcore` (see qcore/build.sh); qcore/__init__.py
// prefers it and falls back to quant.mathutils when it isn't compiled.
//
// The batched implied_vols() releases the GIL so a whole option chain's IV
// inversion runs off the Python interpreter lock — the parallelism win that
// motivated the native kernel.
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>
#include <pybind11/numpy.h>

#include <algorithm>
#include <cmath>
#include <limits>
#include <optional>

namespace py = pybind11;

static const double RISK_FREE = 0.07;
// sqrt(2*pi) with pi to double precision (matches math.sqrt(2*math.pi)).
static const double SQRT_2PI = std::sqrt(2.0 * 3.14159265358979323846);

static inline double norm_cdf(double x) {
    return 0.5 * (1.0 + std::erf(x / std::sqrt(2.0)));
}
static inline double norm_pdf(double x) {
    return std::exp(-0.5 * x * x) / SQRT_2PI;
}

static double bs_price(bool is_call, double spot, double strike, double t,
                       double sigma, double r) {
    if (t <= 0.0 || sigma <= 0.0 || spot <= 0.0 || strike <= 0.0) {
        double intrinsic = is_call ? (spot - strike) : (strike - spot);
        return std::max(0.0, intrinsic);
    }
    double st = sigma * std::sqrt(t);
    double d1 = (std::log(spot / strike) + (r + 0.5 * sigma * sigma) * t) / st;
    double d2 = d1 - st;
    if (is_call)
        return spot * norm_cdf(d1) - strike * std::exp(-r * t) * norm_cdf(d2);
    return strike * std::exp(-r * t) * norm_cdf(-d2) - spot * norm_cdf(-d1);
}

static double bs_gamma(double spot, double strike, double t, double sigma,
                       double r) {
    if (t <= 0.0 || sigma <= 0.0) return 0.0;
    double st = sigma * std::sqrt(t);
    double d1 = (std::log(spot / strike) + (r + 0.5 * sigma * sigma) * t) / st;
    return norm_pdf(d1) / (spot * st);
}

static double bs_vega(double spot, double strike, double t, double sigma,
                      double r) {
    if (t <= 0.0 || sigma <= 0.0) return 0.0;
    double st = sigma * std::sqrt(t);
    double d1 = (std::log(spot / strike) + (r + 0.5 * sigma * sigma) * t) / st;
    return spot * norm_pdf(d1) * std::sqrt(t);
}

// std::nullopt <-> None (matches implied_vol returning None out of bounds).
static std::optional<double> implied_vol(bool is_call, double price,
                                         double spot, double strike, double t,
                                         double r) {
    if (price <= 0.0 || t <= 0.0 || spot <= 0.0 || strike <= 0.0)
        return std::nullopt;
    double intrinsic = std::max(
        0.0, is_call ? (spot - strike) : (strike - spot) * std::exp(-r * t));
    if (price < intrinsic - 1e-9) return std::nullopt;
    double lo = 1e-4, hi = 5.0;
    if (bs_price(is_call, spot, strike, t, hi, r) < price) return std::nullopt;
    for (int i = 0; i < 80; ++i) {
        double mid = 0.5 * (lo + hi);
        if (bs_price(is_call, spot, strike, t, mid, r) < price) lo = mid;
        else hi = mid;
        if (hi - lo < 1e-5) break;
    }
    return 0.5 * (lo + hi);
}

static double years_to_expiry(double expiry_epoch, double now_epoch) {
    double d = expiry_epoch - now_epoch;
    if (d < 0.0) d = 0.0;
    return d / (365.0 * 24.0 * 3600.0);
}

// Batched IV over parallel arrays (one t/r for the whole chain slice). NaN
// marks a leg with no valid solution — callers filter it, same as None. The
// heavy loop runs with the GIL released.
static py::array_t<double> implied_vols(bool is_call, py::array_t<double> price,
                                        py::array_t<double> spot,
                                        py::array_t<double> strike, double t,
                                        double r) {
    auto p = price.unchecked<1>();
    auto s = spot.unchecked<1>();
    auto k = strike.unchecked<1>();
    py::ssize_t n = p.shape(0);
    if (s.shape(0) != n || k.shape(0) != n)
        throw std::runtime_error("implied_vols: price/spot/strike length mismatch");
    py::array_t<double> out(n);
    auto o = out.mutable_unchecked<1>();
    const double nan = std::numeric_limits<double>::quiet_NaN();
    {
        py::gil_scoped_release release;   // pure C++ math — no Python touched
        for (py::ssize_t i = 0; i < n; ++i) {
            auto iv = implied_vol(is_call, p(i), s(i), k(i), t, r);
            o(i) = iv.has_value() ? *iv : nan;
        }
    }
    return out;
}

// SMC order-block / FVG zone lifecycle simulation (batched). Matches
// quant/agents/smc.py SmcZones._simulate bit-for-bit — the O(zones×n) hot loop.
// One call processes every candidate zone with the GIL released. Constants
// (invalidation buffer, mitigation separation/death) are passed in from Python
// so the C++ never drifts from smc.py's params.
//
// Returned state codes: 0 FRESH, 1 MITIGATED, 2 DEAD, 3 INVALIDATED.
// inv_at is the invalidation bar, or -1 for None.
static py::tuple simulate_zones(py::array_t<double> o_, py::array_t<double> h_,
                                py::array_t<double> l_, py::array_t<double> c_,
                                double buf, py::array_t<double> bottoms_,
                                py::array_t<double> tops_,
                                py::array_t<int> is_bull_,
                                py::array_t<int> formed_, int mit_sep,
                                int mit_death) {
    auto o = o_.unchecked<1>();
    auto h = h_.unchecked<1>();
    auto l = l_.unchecked<1>();
    auto c = c_.unchecked<1>();
    auto bot = bottoms_.unchecked<1>();
    auto top = tops_.unchecked<1>();
    auto bull = is_bull_.unchecked<1>();
    auto formed = formed_.unchecked<1>();
    py::ssize_t n = o.shape(0);
    py::ssize_t z = bot.shape(0);
    if (top.shape(0) != z || bull.shape(0) != z || formed.shape(0) != z)
        throw std::runtime_error("simulate_zones: zone array length mismatch");

    py::array_t<int> state(z), mitig(z), weak(z), invat(z);
    auto st = state.mutable_unchecked<1>();
    auto mi = mitig.mutable_unchecked<1>();
    auto wk = weak.mutable_unchecked<1>();
    auto iv = invat.mutable_unchecked<1>();
    {
        py::gil_scoped_release rel;
        for (py::ssize_t zi = 0; zi < z; ++zi) {
            bool is_b = bull(zi) != 0;
            double b = bot(zi), tp = top(zi);
            long f = formed(zi);
            int m = 0, weakened = 0, state_c = 0;      // FRESH
            bool in_ep = false;
            long long out_run = 1000000000LL;          // Python's 10**9 sentinel
            long inv = -1;
            for (py::ssize_t t = f + 1; t < n; ++t) {
                bool touch, fully_out;
                if (is_b) {
                    if (c(t) < b - buf) { state_c = 3; inv = t; break; }
                    if (l(t) < b - buf && c(t) >= b) weakened = 1;
                    touch = l(t) <= tp;
                    fully_out = (l(t) > tp) || (h(t) < b);
                } else {
                    if (c(t) > tp + buf) { state_c = 3; inv = t; break; }
                    if (h(t) > tp + buf && c(t) <= tp) weakened = 1;
                    touch = h(t) >= b;
                    fully_out = (l(t) > tp) || (h(t) < b);
                }
                if (in_ep) {
                    if (fully_out) { in_ep = false; out_run = 1; }
                } else {
                    if (fully_out) {
                        out_run++;
                    } else if (touch) {
                        if (m == 0 || out_run >= mit_sep) {
                            m++;
                            if (m >= mit_death) { state_c = 2; break; }  // DEAD
                        }
                        in_ep = true;
                        out_run = 0;
                    }
                }
            }
            if (state_c == 0 && m >= 1) state_c = 1;   // MITIGATED
            st(zi) = state_c;
            mi(zi) = m;
            wk(zi) = weakened;
            iv(zi) = static_cast<int>(inv);
        }
    }
    return py::make_tuple(state, mitig, weak, invat);
}

PYBIND11_MODULE(_qcore, m) {
    m.doc() = "C++ compute kernel: Black-Scholes + implied vol "
              "(matches quant/mathutils.py) + SMC zone simulation";
    m.attr("RISK_FREE") = RISK_FREE;
    m.def("bs_price", &bs_price, py::arg("is_call"), py::arg("spot"),
          py::arg("strike"), py::arg("t_years"), py::arg("sigma"),
          py::arg("r") = RISK_FREE);
    m.def("bs_gamma", &bs_gamma, py::arg("spot"), py::arg("strike"),
          py::arg("t_years"), py::arg("sigma"), py::arg("r") = RISK_FREE);
    m.def("bs_vega", &bs_vega, py::arg("spot"), py::arg("strike"),
          py::arg("t_years"), py::arg("sigma"), py::arg("r") = RISK_FREE);
    m.def("implied_vol", &implied_vol, py::arg("is_call"), py::arg("price"),
          py::arg("spot"), py::arg("strike"), py::arg("t_years"),
          py::arg("r") = RISK_FREE);
    m.def("implied_vols", &implied_vols, py::arg("is_call"), py::arg("price"),
          py::arg("spot"), py::arg("strike"), py::arg("t_years"),
          py::arg("r") = RISK_FREE);
    m.def("years_to_expiry", &years_to_expiry, py::arg("expiry_epoch"),
          py::arg("now_epoch"));
    m.def("simulate_zones", &simulate_zones, py::arg("o"), py::arg("h"),
          py::arg("l"), py::arg("c"), py::arg("buf"), py::arg("bottoms"),
          py::arg("tops"), py::arg("is_bull"), py::arg("formed"),
          py::arg("mit_sep"), py::arg("mit_death"));
}
