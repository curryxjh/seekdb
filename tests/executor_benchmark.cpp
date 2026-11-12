// Diagnostic only: SQL executor time without stdin/stdout or Python/VM pipes.
// End-to-end acceptance measurements remain in paired_round2.py.
#include "database.h"

#include <chrono>
#include <iostream>
#include <random>

int main(int argc, char **argv) {
  if (argc != 2) return 2;
  mvdb::Database db(argv[1]);
  db.execute(mvdb::parse("CREATE TABLE t(id BIGINT PRIMARY KEY,g BIGINT,v VECTOR(128));"));
  std::mt19937 rng(20261112);
  std::normal_distribution<float> normal;
  const auto vector = [&] {
    std::string out = "'[";
    for (size_t d = 0; d < 128; ++d) {
      if (d) out += ',';
      out += std::to_string(normal(rng));
    }
    return out + "]'";
  };
  for (size_t start = 0; start < 10000; start += 100) {
    std::string sql = "INSERT INTO t VALUES";
    for (size_t i = start; i < start + 100; ++i) {
      if (i != start) sql += ',';
      sql += '(' + std::to_string(i) + ',' + std::to_string(i % 4) + ',' + vector() + ')';
    }
    db.execute(mvdb::parse(sql));
  }
  std::vector<std::string> sql;
  for (size_t i = 0; i < 3000; ++i) {
    sql.push_back("SELECT id,L2_DISTANCE(v," + vector() + ") AS d FROM t" +
                  (i % 2 ? " WHERE g=" + std::to_string(i % 4) : "") + " ORDER BY d LIMIT 10;");
  }
  for (size_t i = 0; i < 10; ++i) db.execute(mvdb::parse(sql[i]));
  using Clock = std::chrono::steady_clock;
  const auto start = Clock::now();
  for (const auto &query : sql) db.execute(mvdb::parse(query));
  const double elapsed = std::chrono::duration<double, std::milli>(Clock::now() - start).count();
  db.shutdown();
  std::cout << "{\"scope\":\"executor+parser only, not end-to-end\",\"seed\":20261112,"
            << "\"rows\":10000,\"dimensions\":128,\"queries\":3000,\"query_ms\":" << elapsed << "}\n";
}
