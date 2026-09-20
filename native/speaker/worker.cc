// Persistent CPU-only CAM++ worker. Protocol: little-endian uint32 rate/count,
// count PCM16 mono samples -> 512 little-endian float32 unit-vector values.
// QNX aarch64le and the development x86_64 target are both little-endian.
#include <onnxruntime_cxx_api.h>
#include "kaldi-native-fbank/csrc/online-feature.h"
#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <iostream>
#include <numeric>
#include <stdexcept>
#include <vector>

static std::vector<float> resample(const std::vector<int16_t>& pcm, uint32_t rate) {
  constexpr double pi = 3.14159265358979323846;
  std::vector<float> out(static_cast<size_t>(pcm.size()) * 16000 / rate);
  if (rate == 16000) {
    for (size_t i = 0; i < out.size(); ++i) out[i] = pcm[i] / 32768.0f;
    return out;
  }
  // Hann-windowed sinc lowpass before downsampling; identical for enrollment/live.
  const double cutoff = std::min(1.0, 16000.0 / rate) * .95;
  constexpr int radius = 48;
  const uint32_t phases = 16000 / std::gcd(rate, uint32_t{16000});
  std::vector<std::array<double, 2 * radius + 1>> kernels(phases);
  for (uint32_t phase = 0; phase < phases; ++phase) {
    double fraction = ((static_cast<uint64_t>(phase) * rate) % 16000) / 16000.0;
    double total = 0;
    for (int k = -radius; k <= radius; ++k) {
      double d = fraction - k, x = pi * cutoff * d;
      double w = std::abs(d) > radius ? 0 : cutoff
          * (std::abs(x) < 1e-12 ? 1 : std::sin(x) / x)
          * (.5 + .5 * std::cos(pi * d / radius));
      kernels[phase][k + radius] = w;
      total += w;
    }
    for (double& weight : kernels[phase]) weight /= total;
  }
  for (size_t i = 0; i < out.size(); ++i) {
    int64_t center = static_cast<uint64_t>(i) * rate / 16000;
    const auto& kernel = kernels[i % phases];
    double value = 0;
    for (int k = -radius; k <= radius; ++k) {
      auto j = std::clamp<int64_t>(center + k, 0, pcm.size() - 1);
      value += pcm[j] * kernel[k + radius];
    }
    out[i] = static_cast<float>(value / 32768.0);
  }
  return out;
}

int main(int argc, char** argv) {
  try {
    if (argc != 2) throw std::runtime_error("usage: speaker-worker MODEL.onnx");
    Ort::Env env(ORT_LOGGING_LEVEL_WARNING, "combadge-speaker");
    env.DisableTelemetryEvents();
    Ort::SessionOptions opts;
    opts.SetIntraOpNumThreads(2);
    opts.SetInterOpNumThreads(1);
    opts.SetGraphOptimizationLevel(GraphOptimizationLevel::ORT_ENABLE_ALL);
    opts.AddConfigEntry("session.intra_op.allow_spinning", "0");
    Ort::Session session(env, argv[1], opts);
    if (session.GetInputCount() != 1 || session.GetOutputCount() != 1)
      throw std::runtime_error("expected the pinned CAM++ embedding model");
    const char* inputs[] = {"x"};
    const char* outputs[] = {"embedding"};
    auto memory = Ort::MemoryInfo::CreateCpu(OrtArenaAllocator, OrtMemTypeDefault);
    std::cout.write("CSP1", 4).flush();
    for (;;) {
      std::array<uint32_t, 2> header{};
      std::cin.read(reinterpret_cast<char*>(header.data()), sizeof(header));
      if (std::cin.gcount() == 0 && std::cin.eof()) return 0;
      if (!std::cin) throw std::runtime_error("truncated request header");
      auto [rate, count] = header;
      if (rate < 8000 || rate > 96000 || count < rate / 2 || count > rate * 10)
        throw std::runtime_error("expected 0.5-10 seconds of mono PCM16 at 8-96kHz");
      std::vector<int16_t> pcm(count);
      std::cin.read(reinterpret_cast<char*>(pcm.data()), count * sizeof(int16_t));
      if (!std::cin) throw std::runtime_error("truncated PCM");
      auto samples = resample(pcm, rate);
      knf::FbankOptions config;
      config.frame_opts.samp_freq = 16000;
      config.frame_opts.dither = 0;
      config.frame_opts.snip_edges = false;
      config.mel_opts.num_bins = 80;
      config.mel_opts.high_freq = -400;
      knf::OnlineFbank fbank(config);
      fbank.AcceptWaveform(16000, samples.data(), samples.size());
      fbank.InputFinished();
      int frames = fbank.NumFramesReady();
      std::vector<float> features(frames * 80);
      for (int i = 0; i < frames; ++i)
        std::copy_n(fbank.GetFrame(i), 80, features.data() + i * 80);
      for (int k = 0; k < 80; ++k) {
        double mean = 0;
        for (int i = 0; i < frames; ++i) mean += features[i * 80 + k];
        mean /= frames;
        for (int i = 0; i < frames; ++i) features[i * 80 + k] -= mean;
      }
      std::array<int64_t, 3> shape{1, frames, 80};
      auto tensor = Ort::Value::CreateTensor<float>(memory, features.data(), features.size(),
                                                   shape.data(), shape.size());
      auto result = session.Run(Ort::RunOptions{nullptr}, inputs, &tensor, 1, outputs, 1);
      if (result[0].GetTensorTypeAndShapeInfo().GetElementCount() != 512)
        throw std::runtime_error("unexpected embedding dimension");
      const float* raw = result[0].GetTensorData<float>();
      double norm = 0;
      for (int i = 0; i < 512; ++i) norm += raw[i] * raw[i];
      if (!std::isfinite(norm) || norm < 1e-12)
        throw std::runtime_error("invalid embedding");
      std::array<float, 512> embedding{};
      for (int i = 0; i < 512; ++i) embedding[i] = raw[i] / std::sqrt(norm);
      std::cout.write(reinterpret_cast<char*>(embedding.data()), sizeof(embedding)).flush();
      if (!std::cout) return 1;
    }
  } catch (const std::exception& e) {
    std::cerr << "Speaker worker: " << e.what() << '\n';
    return 1;
  }
}
