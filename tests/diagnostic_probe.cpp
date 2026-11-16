// Environment/clock probe only. Never linked into the database executable.
#include <cerrno>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <linux/perf_event.h>
#include <sys/syscall.h>
#include <unistd.h>

static unsigned long long now() {
  return static_cast<unsigned long long>(std::chrono::duration_cast<std::chrono::nanoseconds>(
      std::chrono::steady_clock::now().time_since_epoch()).count());
}
int main() {
  perf_event_attr attr{};
  attr.type=PERF_TYPE_HARDWARE; attr.size=sizeof(attr);
  attr.config=PERF_COUNT_HW_CPU_CYCLES;
  attr.disabled=1; attr.exclude_kernel=1; attr.exclude_hv=1;
  const long fd=::syscall(SYS_perf_event_open, &attr, 0, -1, -1, 0);
  const int error=fd<0 ? errno : 0;
  if (fd>=0) ::close(static_cast<int>(fd));
  unsigned long long sum=0, least=~0ULL;
  for (int i=0;i<100000;++i) {
    const auto first=now(), second=now(), delta=second-first;
    sum+=delta; if (delta<least) least=delta;
  }
  bool avx2=false, vnni=false;
#if defined(__x86_64__)
  avx2=__builtin_cpu_supports("avx2");
  vnni=__builtin_cpu_supports("avx512f") &&
       __builtin_cpu_supports("avx512bw") && __builtin_cpu_supports("avx512vnni");
#endif
  std::printf("{\"cpu_avx2\":%s,\"cpu_vnni\":%s,\"cycles_open_errno\":%d,"
              "\"cycles_open_error\":\"%s\",\"cycles_measured\":false,"
              "\"clock_adjacent_min_ns\":%llu,\"clock_adjacent_mean_ns\":%.3f,"
              "\"clock_pairs\":100000}\n",
              avx2 ? "true" : "false",vnni ? "true" : "false",
              error,std::strerror(error),least,static_cast<double>(sum)/100000);
}
