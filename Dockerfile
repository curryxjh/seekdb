FROM gcc:14.2.0-bookworm@sha256:b99b86a28812b1e6453a231a947dc43d76fe192788a12f344a9b568bf9f5d24c AS build
WORKDIR /src
COPY minivectordb/ minivectordb/
RUN g++ -std=c++20 -O3 -Wall -Wextra -Wpedantic -Wconversion -Wshadow \
    -static-libstdc++ -static-libgcc minivectordb/*.cpp -o /minivectordb

FROM debian:bookworm-slim@sha256:734a72b3d6142a71993b4bc580ca01d8ca687f77a2c422d96a961d3376b179bc
COPY --from=build /minivectordb /usr/local/bin/minivectordb
WORKDIR /data
ENTRYPOINT ["/usr/local/bin/minivectordb"]
