"""셀 지도(노드 좌표·구간)와 최단 경로. 계약: 공개 시그니처 유지.

좌표: m, 위에서 본 셀 지도. x = 적재대 길이 방향(도크 쪽 열 1이 작은 x), y = 아래쪽(남쪽)이 +.
노드 이름 규칙 (필수): 'DOCK'(컨베이어 끝 인수점), 'CHARGER', 'PARK1'..'PARKk', 북면 통로 'N01'..'N{cols}', 남면 통로 'S01'..'S{cols}'.
그 밖의 연결 노드는 자유 (예: 'JW', 'JE'). 구간은 양방향, 길이 = 두 노드 직선거리.
"""
from __future__ import annotations

import heapq
import math
from dataclasses import dataclass

from core.config import RackConfig

# 같은 길이 경로 비교용 반올림 자릿수 (부동소수 오차로 동점 판정이 흔들리지 않게)
_ROUND = 9

# build_cell_topology 배치 상수 (m)
_DOCK_X = -0.6            # 도크 x (열 1 바깥쪽, 컨베이어 끝)
_DOCK_DY = 1.0            # 도크 y = 남쪽 통로 y + 이 값
_SERVICE_DY = 1.0         # 충전기·주차 줄 = 북쪽 통로 y − 이 값 (도크 반대편)
_SERVICE_SPACING = 0.9    # 충전기·주차 자리 간격


@dataclass
class Node:
    id: str
    x: float
    y: float
    kind: str = 'junction'   # junction / aisle / dock / charger / park


# 단일 출발점 최단 경로 트리의 한 항목: (반올림 거리, 구간 수, 경로 튜플)
_Label = tuple[float, int, tuple[str, ...]]


class Topology:
    def __init__(self) -> None:
        self._nodes: dict[str, Node] = {}
        self._adj: dict[str, dict[str, float]] = {}
        self._trees: dict[str, dict[str, _Label]] = {}   # 출발 노드별 최단 경로 캐시

    def add_node(self, node: Node) -> None:
        if node.id in self._nodes:
            raise ValueError(f'노드 이름 중복: {node.id}')
        self._nodes[node.id] = node
        self._adj[node.id] = {}
        self._trees.clear()

    def add_edge(self, a: str, b: str) -> None:
        """양방향, 길이 자동."""
        na, nb = self._require(a), self._require(b)
        if a == b:
            raise ValueError(f'같은 노드끼리는 구간을 만들 수 없습니다: {a}')
        length = math.hypot(nb.x - na.x, nb.y - na.y)
        self._adj[a][b] = length
        self._adj[b][a] = length
        self._trees.clear()

    @property
    def nodes(self) -> dict[str, Node]:
        # 내부 사전을 그대로 돌려준다 (읽기 전용으로 쓸 것 — 추가는 add_node 로)
        return self._nodes

    def neighbors(self, node: str) -> list[tuple[str, float]]:
        self._require(node)
        return list(self._adj[node].items())

    def shortest_path(self, a: str, b: str) -> list[str]:
        """a→b 노드 목록 (a, b 포함, a==b 면 [a]). 경로 없으면 ValueError."""
        self._require(a)
        self._require(b)
        if a == b:
            return [a]
        label = self._tree(a).get(b)
        if label is None:
            raise ValueError(f'경로 없음: {a} → {b}')
        return list(label[2])

    def distance(self, a: str, b: str) -> float:
        """경로 길이 (m). 경로 없으면 inf."""
        if a not in self._nodes or b not in self._nodes:
            return math.inf
        if a == b:
            return 0.0
        label = self._tree(a).get(b)
        if label is None:
            return math.inf
        return self.path_length(list(label[2]))

    def path_length(self, path: list[str]) -> float:
        total = 0.0
        for p, q in zip(path, path[1:]):
            try:
                total += self._adj[p][q]
            except KeyError:
                raise ValueError(f'구간 없음: {p} – {q}') from None
        return total

    def bounds(self) -> tuple[float, float, float, float]:
        """(min_x, min_y, max_x, max_y) — 미니맵 그리기용."""
        if not self._nodes:
            return (0.0, 0.0, 0.0, 0.0)
        xs = [n.x for n in self._nodes.values()]
        ys = [n.y for n in self._nodes.values()]
        return (min(xs), min(ys), max(xs), max(ys))

    # ---- 내부
    def _require(self, node: str) -> Node:
        try:
            return self._nodes[node]
        except KeyError:
            raise ValueError(f'지도에 없는 노드: {node}') from None

    def _tree(self, src: str) -> dict[str, _Label]:
        """Dijkstra. 동점이면 구간 수가 적은 경로, 그래도 같으면 노드 이름 순서가 앞선 경로 (결정적)."""
        cached = self._trees.get(src)
        if cached is not None:
            return cached
        best: dict[str, _Label] = {src: (0.0, 0, (src,))}
        heap: list[_Label] = [(0.0, 0, (src,))]
        while heap:
            label = heapq.heappop(heap)
            node = label[2][-1]
            if best.get(node) != label:
                continue   # 더 좋은 경로가 이미 확정됨
            dist, hops, path = label
            for nb, length in self._adj[node].items():
                cand: _Label = (round(dist + length, _ROUND), hops + 1, path + (nb,))
                cur = best.get(nb)
                if cur is None or cand < cur:
                    best[nb] = cand
                    heapq.heappush(heap, cand)
        self._trees[src] = best
        return best


def build_cell_topology(rack: RackConfig, parks: int = 2) -> Topology:
    """적재대 설정으로 셀 지도 생성: 북·남 통로 노드(열마다), 양 끝 연결, 도크(남쪽 열 1 근처, 컨베이어 끝),
    충전기·주차 노드(도크 반대쪽 또는 옆, 도크·통로를 막지 않는 곳). 모든 노드가 서로 연결되어야 한다."""
    if rack.columns < 1:
        raise ValueError('적재대 열 수는 1 이상이어야 합니다')
    if parks < 0:
        raise ValueError('주차 자리 수는 0 이상이어야 합니다')

    topo = Topology()
    cols = rack.columns
    pitch = rack.col_pitch_m
    length = cols * pitch
    half_depth = rack.depth_m / 2
    y_north = -(half_depth + rack.aisle_m / 2)   # 북면 통로 중심선
    y_south = +(half_depth + rack.aisle_m / 2)   # 남면 통로 중심선
    end_gap = rack.aisle_m / 2 + 0.15            # 적재대 끝에서 회전 교차점까지
    x_west, x_east = -end_gap, length + end_gap

    # 통로 노드 (열 정면)
    north = [f'N{c:02d}' for c in range(1, cols + 1)]
    south = [f'S{c:02d}' for c in range(1, cols + 1)]
    for c in range(1, cols + 1):
        x = (c - 0.5) * pitch
        topo.add_node(Node(north[c - 1], x, y_north, 'aisle'))
        topo.add_node(Node(south[c - 1], x, y_south, 'aisle'))
    for row in (north, south):
        for a, b in zip(row, row[1:]):
            topo.add_edge(a, b)

    # 양 끝 교차점: 북 통로 ↔ 남 통로를 적재대 바깥으로 돌아서 연결
    topo.add_node(Node('JNW', x_west, y_north))
    topo.add_node(Node('JSW', x_west, y_south))
    topo.add_node(Node('JNE', x_east, y_north))
    topo.add_node(Node('JSE', x_east, y_south))
    topo.add_edge('JNW', north[0])
    topo.add_edge('JSW', south[0])
    topo.add_edge('JNE', north[-1])
    topo.add_edge('JSE', south[-1])
    topo.add_edge('JNW', 'JSW')
    topo.add_edge('JNE', 'JSE')

    # 도크: 남쪽, 열 1 바깥 (컨베이어 끝). 통로 밖이라 지나가는 경로가 생기지 않는다.
    topo.add_node(Node('DOCK', _DOCK_X, y_south + _DOCK_DY, 'dock'))
    topo.add_edge('DOCK', south[0])
    topo.add_edge('DOCK', 'JSW')

    # 충전기·주차: 도크 반대편(북쪽 통로 바깥) 한 줄, 동쪽 끝부터. 각각 가장 가까운 북쪽 노드 하나에만 붙는
    # 막다른 노드라서 다른 경로의 중간에 끼지 않는다 (통로·도크를 막지 않음).
    y_service = y_north - _SERVICE_DY
    north_side = ['JNW'] + north + ['JNE']
    service = [('CHARGER', 'charger')] + [(f'PARK{i}', 'park') for i in range(1, parks + 1)]
    for i, (name, kind) in enumerate(service):
        x = x_east - i * _SERVICE_SPACING
        topo.add_node(Node(name, x, y_service, kind))
        anchor = min(north_side, key=lambda n: (abs(topo.nodes[n].x - x), north_side.index(n)))
        topo.add_edge(name, anchor)
    return topo
