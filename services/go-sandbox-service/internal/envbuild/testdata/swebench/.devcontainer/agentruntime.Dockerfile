FROM swebench/sweb.eval.x86_64.acme_1776_calc-1:latest
# agentruntime swebench environment v2
ENV PATH=/opt/miniconda3/envs/testbed/bin:/opt/miniconda3/bin:$PATH CONDA_DEFAULT_ENV=testbed
RUN rm -rf /testbed && ln -s /workspace /testbed
