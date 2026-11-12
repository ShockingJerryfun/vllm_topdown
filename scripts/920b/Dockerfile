FROM vllm-qwen3-8b:0.26.0-cu130-opt

# Runtime additions copied from the verified 84 container during packaging.
COPY runtime/numactl /usr/bin/numactl
COPY runtime/libnuma.so.1 /usr/lib64/libnuma.so.1
COPY runtime/site-packages/ /opt/vllm/lib/python3.13/site-packages/
