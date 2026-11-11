"""Record this observed development session without inventing student evidence.

This is an explicit one-session recorder, not an automatic transcript generator.
Run only after the named reports exist and have passed. It rejects duplicates.
"""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    path = ROOT / 'agent/conversation.json'
    record = json.loads(path.read_text(encoding='utf-8'))
    if any(item['id'] == 'student-002' for item in record['conversations']):
        raise SystemExit('session already recorded; edit with real subsequent evidence instead')
    public = (ROOT / 'protocol-results/docker-final-002/result.json').read_text(encoding='utf-8')
    stress = (ROOT / 'local-results/stress-final-128m/result.json').read_text(encoding='utf-8')
    assert json.loads(public)['passed'] is True
    assert json.loads(stress)['passed'] is True
    record['scope'] = '两次真实用户请求对应的仓库初始化与首版数据库开发；包含必要题目上下文、Agent 判断及实际验证摘要。不是完整逐字工具日志导出，不含隐藏思维链。'
    record['conversations'].extend([
        {'id': 'student-002', 'role': 'student', 'source': 'actual_user_message', 'content': '整 个赛题内容 开始把'},
        {'id': 'agent-002', 'role': 'agent', 'source': 'actual_agent_commentary', 'content': '核心存储将自行实现：用磁盘哈希索引定位记录，事务修改先写入临时文件，提交日志完整落盘后再更新可见状态。这样可以避免把全库装进内存，也能在恢复时丢弃未完成提交。'},
        {'id': 'agent-003', 'role': 'agent', 'source': 'actual_agent_commentary', 'content': '我还发现索引启动流程需要补强：当前内存加速索引按需构建，而题目要求索引重建完成后才能报告就绪。我会增加磁盘向量索引，在日志恢复时同步重建；内存缓存继续作为加速层。'}
    ])
    record['experiments'].extend([
        {'id': 'tool-005', 'source': 'actual_tool_output_summary', 'kind': 'linux_extended_tests', 'command': '.venv/bin/python -m unittest discover -s tests -v', 'output': 'PUBLIC: 20/20 cases, 95 SQL assertions\nDIFFERENTIAL: 350 seeded operations and filtered TopK cross-checks passed\nRECOVERY: 140 commit byte prefixes recovered atomically\nRan 13 tests in 44.999s\nOK', 'scope': 'Linux/WSL 测试包含 Unicode/BIGINT/float32、语句与事务、索引多块查询、磁盘归并和缓存回退；为真实工具输出的必要摘要，不伪称完整逐字日志。'},
        {'id': 'tool-006', 'source': 'actual_report_file_contents', 'kind': 'docker_public_cases', 'command': 'protocol_check/run.py --image sha256:08b882bbbb121b6cdcc4a54741e319b7d0da5ad1c57b0e733d267fa97dc6a954 --output protocol-results/docker-final-002', 'output': public},
        {'id': 'tool-007', 'source': 'actual_report_file_contents', 'kind': 'container_resource_and_recovery', 'command': 'scripts/stress_docker.py --output local-results/stress-final-128m', 'output': stress},
        {'id': 'tool-008', 'source': 'actual_report_file_contents', 'kind': 'linux_local_indexed_benchmark', 'output': (ROOT / 'local-results/benchmark-indexed.json').read_text(encoding='utf-8')},
        {'id': 'tool-009', 'source': 'actual_report_file_contents', 'kind': 'linux_local_exact_benchmark', 'output': (ROOT / 'local-results/benchmark-linux-exact.json').read_text(encoding='utf-8')},
        {'id': 'tool-010', 'source': 'actual_tool_output_excerpt', 'kind': 'failed_dependency_download', 'output': 'Downloading numpy ... 5.5/16.5 MB\nExpected sha256 fd83c01228a688733f1ded5201c678f0c53ecc1006ffbc404db9f7a899ac6249\nGot bdfbc53adefcc4fd9984712919701cce01c43dc5e17cca63daac2acc9cf3a028\nERROR: THESE PACKAGES DO NOT MATCH THE HASHES FROM THE REQUIREMENTS FILE.'},
        {'id': 'tool-011', 'source': 'actual_tool_output_excerpt', 'kind': 'dependency_correction_and_retest', 'output': 'Get-FileHash: FD83C01228A688733F1DED5201C678F0C53ECC1006FFBC404DB9F7A899AC6249\nRUN pip install --no-index --find-links=/app/vendor --require-hashes ...\nSuccessfully installed numpy-2.2.6\nDocker build with --network=none: exit_code=0'},
        {'id': 'tool-012', 'source': 'actual_tool_output_excerpt', 'kind': 'incorrect_image_identifier', 'output': 'docker: Error response from daemon: No such image: sha256:011107bfce109a0e1fe9e3922ec2fe79684bba3c9ff468eb35b37ecc2d1e58b6\ndocker image inspect minivectordb:local --format {{.Id}}\nsha256:08b882bbbb121b6cdcc4a54741e319b7d0da5ad1c57b0e733d267fa97dc6a954'}
    ])
    record['agent_assessments'].extend([
        {'id': 'assessment-003', 'author': 'agent', 'status': 'implemented_and_locally_tested', 'content': '首版包含严格 SQL 子集、磁盘哈希存储、独立语句暂存、显式事务、checksummed 提交帧、恢复和磁盘平面向量索引。内存加速缓存有预算，磁盘排序与流式 TopK 提供回退。公开测试与补充测试是本地证据，不等于全部正式准入。'},
        {'id': 'assessment-004', 'author': 'agent', 'status': 'correction_and_retest', 'content': '识别到仅按需创建内存向量索引与 ready 前重建要求有差异，增加随日志恢复重建的磁盘平面索引，并复测事务覆盖层、过滤、更新、删除和重启。此问题发现与实现均为 Agent 行为，不是学生独立质疑。'},
        {'id': 'assessment-005', 'author': 'agent', 'status': 'correction_and_retest', 'content': '依赖下载截断引发哈希失败，保留官方哈希，取得校验匹配的完整 wheel 随仓库安装，离线 Docker 构建通过。一次测试镜像参数误用 config 摘要，inspect 获取实际镜像 ID 后重测公开用例通过。'},
        {'id': 'assessment-006', 'author': 'agent', 'status': 'measurement_interpretation', 'content': '196608000 字节文本数据大于 134217728 字节容器内存上限，负载通过。最终重启 memory.peak=70361088、oom_kill=0，只覆盖最后一次容器生命周期；不能把它当作全部装载过程峰值。定时强杀不证明恢复的特定指令被中断。'}
    ])
    record['development_state'] = {'implemented': 'SQL/类型约束、CRUD、过滤精确 TopK、磁盘索引、事务、正常关闭及进程强杀恢复、Docker 封装', 'local_validation': '20/20 公开样例，95 SQL；13 扩展测试；196.6 MB 数据 / 128 MiB 内存容器检查通过', 'official_evaluation': '未执行；没有正式成绩', 'pending': '日志压缩、正式资源与错误码最终适配、平台分支名核实、正式预检与准入'}
    record['retrospective'] = [{'author': 'agent', 'content': '正确性先于性能；以日志作为唯一权威避免记录和向量索引出现不同提交版本。包围盒索引与精确扫描均保留过滤和主键并列规则。成本是日志增长和启动重放，后续须用相同资源条件下的重复测量评估压缩与索引收益。学生尚未提供自己的解释或独立实验反馈，不替学生作反思。'}]
    record['limitations'] = [
        '学生原话只有任务要求与开始开发的指令，没有技术机制解释、独立预测、反例、实验解释或复盘；Agent 不代写学生理解。',
        '早期 tool-003 记录的是初始化时 Docker 未启动的实际状态；开发阶段已启动并完成容器验证。早期 plans 字段是当时尚未执行的规划，后续实际状态见 experiments 和 development_state。',
        '没有组织方全部正式用例、三轮端到端性能基线或 Agent 评分；公开通过不能替代正式准入。',
        '性能烟测只独立核对前 10 次查询；不同 OS、单轮、并行本地负载和包含生成器的 RSS 不支持官方性能结论。',
        '日志尚无 checkpoint/压缩；高频更新的磁盘增长与恢复时长需要进一步验证。',
        '这里保留足以核对的真实片段与结果摘要，没有声称包含完整历史会话；不记录密钥或隐藏思维链。'
    ]
    raw = (json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')
    assert len(raw) <= 128 * 1024
    path.write_bytes(raw)
    print(f'Recorded actual development evidence: {len(raw)} bytes')


if __name__ == '__main__':
    main()
