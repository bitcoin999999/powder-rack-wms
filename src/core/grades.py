"""재종 기준정보 샘플.

ISO 513 피삭재 분류 색(P 파랑, M 노랑, K 빨강, N 초록, S 주황·갈색, H 회색) 계열로 재종마다 색을 다르게 했다.
코드·ISO 범위는 인써트 공정에서 흔히 쓰는 형태로 만든 **샘플**이다 — 사내 재종표로 교체한다(기준정보 탭).
"""
from __future__ import annotations

from .models import Grade

SAMPLE_GRADES: list[Grade] = [
    # P — 강
    Grade('NC3010', 'P', 'P01–P15', 'CVD', '강 고속 정삭·중절삭', '#1f5fbf'),
    Grade('NC3020', 'P', 'P10–P25', 'CVD', '강 범용 선삭', '#2f74d6'),
    Grade('NC3030', 'P', 'P20–P35', 'CVD', '강 황삭·단속', '#4a8de8'),
    Grade('NC3215', 'P', 'P10–P25', 'CVD', '강 범용 (고경도 기판)', '#1a4c94'),
    Grade('NC3225', 'P', 'P15–P30', 'CVD', '강 중절삭', '#5b9ff0'),
    Grade('PC3700', 'P', 'P20–P40', 'PVD', '강 밀링·홈가공', '#7fb3f5'),
    # M — 스테인리스
    Grade('NC9020', 'M', 'M10–M25', 'CVD', '스테인리스 정삭·중절삭', '#d9a400'),
    Grade('PC9030', 'M', 'M20–M35', 'PVD', '스테인리스 범용', '#f2bf1a'),
    Grade('NC5330', 'M', 'M20–M35', 'CVD', '스테인리스 황삭', '#b88a00'),
    Grade('PC5300', 'M', 'M15–M30', 'PVD', '스테인리스·강 밀링', '#f5d45c'),
    # K — 주철
    Grade('NC6205', 'K', 'K01–K10', 'CVD', '회주철 고속', '#c62828'),
    Grade('NC6215', 'K', 'K05–K20', 'CVD', '주철 범용', '#e04848'),
    Grade('PC8110', 'K', 'K05–K20', 'PVD', '주철 밀링', '#a31f1f'),
    # N — 비철
    Grade('H01', 'N', 'N01–N10', '무코팅', '알루미늄·비철 정삭', '#2e9d4e'),
    Grade('H05', 'N', 'N05–N15', '무코팅', '비철 범용', '#5cbf74'),
    # S — 내열합금
    Grade('PC5400', 'S', 'S10–S25', 'PVD', '내열합금·티타늄', '#c8641e'),
    Grade('PC9530', 'S', 'S15–S30', 'PVD', '내열합금 황삭', '#8f4a16'),
    # H — 고경도
    Grade('PC6510', 'H', 'H05–H20', 'PVD', '고경도강 정삭', '#7a7f87'),
]


def sample_lot(grade: str, seq: int, yymm: str = '2610') -> str:
    """샘플 로트 번호. 예) L2610-PC9030-0012"""
    return f'L{yymm}-{grade}-{seq:04d}'
