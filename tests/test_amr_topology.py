import itertools
import math

import pytest

from amr.topology import Node, Topology, build_cell_topology
from core.config import RackConfig

RACK_SIZES = [
    RackConfig(),                                  # 현장 기본: 10열
    RackConfig(columns=1),
    RackConfig(columns=3, col_pitch_m=0.5),
    RackConfig(columns=16, depth_m=1.2, aisle_m=1.5),
]
SERVICE = ('DOCK', 'CHARGER')


def _reachable(topo: Topology, start: str) -> set[str]:
    seen, todo = {start}, [start]
    while todo:
        for nb, _ in topo.neighbors(todo.pop()):
            if nb not in seen:
                seen.add(nb)
                todo.append(nb)
    return seen


@pytest.mark.parametrize('rack', RACK_SIZES, ids=lambda r: f'{r.columns}cols')
@pytest.mark.parametrize('parks', [0, 2, 5])
def test_required_nodes_and_connectivity(rack, parks):
    topo = build_cell_topology(rack, parks=parks)
    names = set(topo.nodes)
    required = {'DOCK', 'CHARGER'} | {f'PARK{i}' for i in range(1, parks + 1)}
    required |= {f'{face}{c:02d}' for face in 'NS' for c in range(1, rack.columns + 1)}
    assert required <= names
    assert f'PARK{parks + 1}' not in names
    assert _reachable(topo, 'DOCK') == names          # 모두 연결
    for nid, node in topo.nodes.items():
        assert node.id == nid
    assert topo.nodes['DOCK'].kind == 'dock'
    assert topo.nodes['CHARGER'].kind == 'charger'
    assert all(topo.nodes[f'PARK{i}'].kind == 'park' for i in range(1, parks + 1))
    assert all(topo.nodes[f'N{c:02d}'].kind == 'aisle' for c in range(1, rack.columns + 1))


@pytest.mark.parametrize('rack', RACK_SIZES, ids=lambda r: f'{r.columns}cols')
def test_geometry(rack):
    topo = build_cell_topology(rack, parks=3)
    half = rack.depth_m / 2 + rack.aisle_m / 2
    for c in range(1, rack.columns + 1):
        n, s = topo.nodes[f'N{c:02d}'], topo.nodes[f'S{c:02d}']
        assert n.x == pytest.approx((c - 0.5) * rack.col_pitch_m)
        assert s.x == pytest.approx(n.x)
        assert n.y == pytest.approx(-half)
        assert s.y == pytest.approx(half)
    dock = topo.nodes['DOCK']
    assert dock.y > topo.nodes['S01'].y                 # 남쪽 통로 바깥
    assert dock.x < topo.nodes['S01'].x                 # 열 1 쪽 (컨베이어 끝)
    for name in ['CHARGER', 'PARK1', 'PARK2', 'PARK3']:
        assert topo.nodes[name].y < topo.nodes['N01'].y  # 도크 반대편 (북쪽 바깥)
    # 서비스 노드끼리 자리가 겹치지 않음
    pts = {(round(topo.nodes[n].x, 6), round(topo.nodes[n].y, 6)) for n in ['DOCK', 'CHARGER', 'PARK1', 'PARK2', 'PARK3']}
    assert len(pts) == 5
    min_x, min_y, max_x, max_y = topo.bounds()
    assert min_x <= 0 and max_x >= rack.columns * rack.col_pitch_m
    assert min_y < -half and max_y > half


@pytest.mark.parametrize('rack', RACK_SIZES, ids=lambda r: f'{r.columns}cols')
def test_service_nodes_never_on_other_paths(rack):
    """도크·충전기·주차 노드는 다른 두 노드 사이 최단 경로의 중간에 끼지 않는다 (통로·도크를 막지 않음)."""
    topo = build_cell_topology(rack, parks=3)
    service = {'DOCK', 'CHARGER', 'PARK1', 'PARK2', 'PARK3'}
    for a, b in itertools.permutations(topo.nodes, 2):
        path = topo.shortest_path(a, b)
        assert not service & set(path[1:-1]), (a, b, path)
    # 도크에서 통로로 가는 길에 충전기·주차가 없다
    for c in range(1, rack.columns + 1):
        for face in 'NS':
            path = topo.shortest_path('DOCK', f'{face}{c:02d}')
            assert not {'CHARGER', 'PARK1', 'PARK2', 'PARK3'} & set(path)


def test_shortest_path_basic():
    topo = build_cell_topology(RackConfig(), parks=2)
    assert topo.shortest_path('N03', 'N03') == ['N03']
    assert topo.shortest_path('N01', 'N04') == ['N01', 'N02', 'N03', 'N04']
    assert topo.shortest_path('S10', 'S07') == ['S10', 'S09', 'S08', 'S07']
    path = topo.shortest_path('DOCK', 'S03')
    assert path == ['DOCK', 'S01', 'S02', 'S03']
    # 북쪽 통로는 서쪽 끝을 돌아서 간다 (열 1 쪽이 더 가까움)
    path = topo.shortest_path('DOCK', 'N02')
    assert path == ['DOCK', 'JSW', 'JNW', 'N01', 'N02']
    assert topo.path_length(path) == pytest.approx(topo.distance('DOCK', 'N02'))
    # 반대 방향 길이 같음
    assert topo.distance('N02', 'DOCK') == pytest.approx(topo.distance('DOCK', 'N02'))
    assert topo.distance('N05', 'N05') == 0.0


def test_shortest_path_matches_brute_force():
    """작은 지도에서 모든 단순 경로를 다 뒤져 찾은 최단 길이와 같다."""
    topo = build_cell_topology(RackConfig(columns=3), parks=1)
    names = list(topo.nodes)

    def brute(a, b):
        best = math.inf
        stack = [(a, [a], 0.0)]
        while stack:
            node, path, length = stack.pop()
            if node == b:
                best = min(best, length)
                continue
            for nb, w in topo.neighbors(node):
                if nb not in path and length + w < best:
                    stack.append((nb, path + [nb], length + w))
        return best

    for a, b in itertools.combinations(names, 2):
        assert topo.distance(a, b) == pytest.approx(brute(a, b)), (a, b)


def test_manual_topology_ties_and_errors():
    topo = Topology()
    # 정사각형: A→C 두 경로 길이 같음 → 이름 순서가 앞선 B 쪽
    topo.add_node(Node('A', 0, 0))
    topo.add_node(Node('B', 1, 0))
    topo.add_node(Node('D', 0, 1))
    topo.add_node(Node('C', 1, 1))
    topo.add_edge('A', 'D')
    topo.add_edge('A', 'B')
    topo.add_edge('B', 'C')
    topo.add_edge('D', 'C')
    for _ in range(3):
        assert topo.shortest_path('A', 'C') == ['A', 'B', 'C']
    assert topo.distance('A', 'C') == pytest.approx(2.0)
    assert sorted(topo.neighbors('A')) == [('B', 1.0), ('D', 1.0)]
    assert topo.path_length(['A', 'B', 'C']) == pytest.approx(2.0)
    assert topo.path_length(['A']) == 0.0

    topo.add_node(Node('X', 5, 5))     # 떨어진 노드
    with pytest.raises(ValueError):
        topo.shortest_path('A', 'X')
    assert topo.distance('A', 'X') == math.inf
    assert topo.distance('A', 'nowhere') == math.inf
    with pytest.raises(ValueError):
        topo.shortest_path('A', 'nowhere')
    with pytest.raises(ValueError):
        topo.path_length(['A', 'C'])   # 구간 없음
    with pytest.raises(ValueError):
        topo.add_node(Node('A', 9, 9))
    with pytest.raises(ValueError):
        topo.add_edge('A', 'nowhere')
    # 구간 추가 후 캐시 갱신
    topo.add_edge('C', 'X')
    assert topo.shortest_path('A', 'X') == ['A', 'B', 'C', 'X']
    assert Topology().bounds() == (0.0, 0.0, 0.0, 0.0)


def test_bad_arguments():
    with pytest.raises(ValueError):
        build_cell_topology(RackConfig(columns=0))
    with pytest.raises(ValueError):
        build_cell_topology(RackConfig(), parks=-1)
