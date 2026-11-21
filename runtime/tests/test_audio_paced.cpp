// SPDX-License-Identifier: GPL-3.0-or-later
// The paced sink (src/services/audio_paced.cpp): a simulated output device that drains its queue at the sample rate on the host clock.
// The audio renderer paces itself by the queue level of such a device (AudioProcessor::Wait), so these cases pin down what the sink reports.
#include <chrono>
#include <cstdio>
#include <cstdint>
#include <memory>
#include <string>
#include <thread>
#include <vector>

#include "nxrt/audio_sink.h"
#include "test_framework.h"

NXTEST_GROUP(svc_audio_paced)

using namespace nxrt;

namespace {
constexpr uint32_t kRate = 48000;
using Clock = std::chrono::steady_clock;
void sleep_ms(int ms) { std::this_thread::sleep_for(std::chrono::milliseconds(ms)); }
double ms_between(Clock::time_point a, Clock::time_point b) { return std::chrono::duration<double, std::milli>(b - a).count(); }
// The timed cases sleep a few tens of milliseconds into a 100 ms queue. On a loaded machine (the whole suite in one process, other
// programs) a sleep can overshoot until the queue runs dry, which says nothing about the sink: such an attempt is measured, thrown away
// and run again, and the case is skipped if no attempt was timed well enough.
constexpr int kAttempts = 5;
}  // namespace

NXTEST(paced_has_a_device_clock_and_the_null_sink_does_not) {
    auto p = audio::make_paced_sink();
    CHECK(p->has_device_clock());
    CHECK_EQ(std::string(p->name()), std::string("paced"));
    audio::NullSink n;
    CHECK(!n.has_device_clock());
}

NXTEST(paced_queue_drains_at_the_sample_rate) {
    std::vector<int16_t> pcm(static_cast<size_t>(kRate / 10) * 2, 0);   // 100 ms
    for (int attempt = 0; attempt < kAttempts; ++attempt) {
        auto p = audio::make_paced_sink();
        REQUIRE(p->open(kRate, 2));
        const Clock::time_point t0 = Clock::now();
        CHECK_EQ(p->write(pcm.data(), kRate / 10), kRate / 10);
        const Clock::time_point t1 = Clock::now();
        const uint32_t q0 = p->queued_frames();
        sleep_ms(50);
        const Clock::time_point t2 = Clock::now();
        const uint32_t q = p->queued_frames();
        const Clock::time_point t3 = Clock::now();
        const double most = ms_between(t0, t3), least = ms_between(t1, t2);   // how long the queue has drained, at most and at least
        if (most > 85.0) {   // the sleep overshot toward the end of the queue: not a measurement of the sink
            std::printf("    attempt %d: %.1f ms passed instead of about 50, measuring again\n", attempt + 1, most);
            p->close();
            continue;
        }
        CHECK(q0 > kRate / 10 - 480 - static_cast<uint32_t>(ms_between(t0, t3) * kRate / 1000));   // just written: still about 100 ms
        // what is left matches the time that passed, within 10 ms (the device clock's granularity and the read itself)
        const double lo = kRate / 10 - most * kRate / 1000 - 480, hi = kRate / 10 - least * kRate / 1000 + 480;
        CHECK_MSG(q >= lo && q <= hi, "queued " + std::to_string(q) + ", expected " + std::to_string(lo) + ".." + std::to_string(hi));
        const audio::SinkStats s = p->stats();
        CHECK_EQ(s.underruns, 0ull);
        CHECK_EQ(s.frames_accepted, static_cast<uint64_t>(kRate / 10));
        p->close();
        return;
    }
    nxtest::skip("the machine is too busy to time a 50 ms sleep");
}

NXTEST(paced_counts_one_underrun_per_dry_stretch) {
    auto p = audio::make_paced_sink();
    REQUIRE(p->open(kRate, 2));
    std::vector<int16_t> pcm(static_cast<size_t>(480) * 2, 0);          // 10 ms
    p->write(pcm.data(), 480);
    sleep_ms(60);                                                       // the queue is empty for 50 ms: one underrun
    CHECK_EQ(p->queued_frames(), 0u);
    CHECK_EQ(p->queued_frames(), 0u);                                   // asking again does not count again
    CHECK_EQ(p->stats().underruns, 1ull);
    p->write(pcm.data(), 480);                                          // playback resumes
    sleep_ms(60);
    CHECK_EQ(p->stats().underruns, 2ull);
    p->close();
}

NXTEST(paced_drops_what_does_not_fit) {
    auto p = audio::make_paced_sink();
    REQUIRE(p->open(kRate, 2));
    std::vector<int16_t> pcm(static_cast<size_t>(kRate) * 2, 0);        // one second at once: the queue holds 120 ms
    const uint32_t took = p->write(pcm.data(), kRate);
    CHECK(took > kRate / 10 && took < kRate / 5);
    const audio::SinkStats s = p->stats();
    CHECK_EQ(s.frames_written, static_cast<uint64_t>(kRate));
    CHECK_EQ(s.frames_accepted + s.frames_dropped, static_cast<uint64_t>(kRate));
    p->close();
}

NXTEST(paced_skew_changes_the_drain_rate) {
    std::vector<int16_t> pcm(static_cast<size_t>(kRate / 10) * 2, 0);
    for (int attempt = 0; attempt < kAttempts; ++attempt) {
        auto fast = audio::make_paced_sink(100000.0);   // the device runs 10 % fast
        auto slow = audio::make_paced_sink(-100000.0);  // and 10 % slow
        REQUIRE(fast->open(kRate, 2) && slow->open(kRate, 2));
        const Clock::time_point t0 = Clock::now();
        fast->write(pcm.data(), kRate / 10);
        slow->write(pcm.data(), kRate / 10);
        sleep_ms(50);
        const uint32_t qs = slow->queued_frames(), qf = fast->queued_frames();
        const double took = ms_between(t0, Clock::now());
        fast->close();
        slow->close();
        // the fast device empties its 100 ms after about 91 ms of the host clock: past that both may read 0
        if (took > 85.0) {
            std::printf("    attempt %d: %.1f ms passed instead of about 50, measuring again\n", attempt + 1, took);
            continue;
        }
        // 50 ms of the host clock: the fast device consumed about 55 ms, the slow one about 45 ms (480 frames apart)
        CHECK_MSG(qs > qf, "slow " + std::to_string(qs) + " frames queued, fast " + std::to_string(qf));
        return;
    }
    nxtest::skip("the machine is too busy to time a 50 ms sleep");
}
