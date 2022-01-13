import geopandas as gpd  
import pandas as pd
from shapely.geometry import Polygon,Point
from .grids import GPS_to_grids,grids_centre
import math 
import numpy as np
from .preprocess import *

def busgps_arriveinfo(data,line,stop,col = ['VehicleId','GPSDateTime','lon','lat','stopname'],
                      stopbuffer = 200,mintime = 300,project_epsg = 2416,timegap = 1800,method = 'project',projectoutput = False):
    '''
    Input bus GPS data, bus route and station GeoDataFrame, this method can identify the bus arrival and departure information
    
    Parameters
    -------
    data : DataFrame
        Bus GPS data. It should be the data from one bus route, and need to contain vehicle ID, GPS time, latitude and longitude (wgs84)
    line : GeoDataFrame
        GeoDataFrame for the bus line
    stop : GeoDataFrame
        GeoDataFrame for bus stops
    col : List
        Column names, in the order of [vehicle ID, time, longitude, latitude, station name]
    stopbuffer : number
        Meter. When the vehicle approaches the station within this certain distance, it is considered to be arrive at the station.
    mintime : number
        Seconds. Within a short period of time that the bus arrive at bus station again, it will not be consider as another arrival
    project_epsg : number
        The matching algorithm will convert the data into a projection coordinate system to calculate the distance, here the epsg code of the projection coordinate system is given
    timegap : number
        Seconds. For how long the vehicle does not appear, it will be considered as a new vehicle
    method : str
        The method of matching the bus, either ‘project’ or ‘dislimit’; ‘project’ is to directly match the nearest point on the route, which is fast. ‘dislimit’ needs to consider the front point position with the distance limitation, the matching speed is slow.
    projectoutput : bool
        Whether to output the projected data

    Returns
    -------
    arrive_info : DataFrame
        Bus arrival and departure information
    '''
    VehicleId,GPSDateTime,lon,lat,stopcol = col
    #数据清洗
    print('Cleaning data',end = '')
    line.set_crs(crs='epsg:4326',allow_override=True,inplace=True)
    line = line.to_crs(epsg = project_epsg)
    line_buffer = line.copy()
    line_buffer['geometry'] = line_buffer.buffer(200)
    line_buffer = line_buffer.to_crs(epsg = 4326)
    print('.',end = '')
    data = clean_same(data,col=[VehicleId,GPSDateTime,lon,lat])
    print('.',end = '')
    data = clean_outofshape(data,line_buffer,col = [lon,lat],accuracy = 500)
    print('.')
    data = id_reindex(data,VehicleId,timegap = timegap,timecol = GPSDateTime,suffix='')

    print('Position matching',end = '')
    #利用project方法，将数据点投影至公交线路上
    lineshp = line['geometry'].iloc[0]
    print('.',end = '')
    data['geometry'] = gpd.points_from_xy(data[lon],data[lat])
    data = gpd.GeoDataFrame(data)
    data.set_crs(crs='epsg:4326',allow_override=True,inplace=True)
    print('.',end = '')
    data = data.to_crs(epsg = project_epsg)
    print('.',end = '')
    if method == 'project':
        data['project'] = data['geometry'].apply(lambda r:lineshp.project(r))
    elif method == 'dislimit':
        tmps = []
        #改进的匹配方法
        for vid in data[VehicleId].drop_duplicates():
            print('.',end = '')
            tmp = data[data[VehicleId]==vid].copy()
            gap = 30
            i = 0
            tmp = tmp.sort_values(by = [VehicleId,GPSDateTime]).reset_index(drop=True)
            tmp['project'] = 0
            from shapely.geometry import LineString
            for i in range(len(tmp)-1):
                if i == 0:
                    proj = lineshp.project(tmp.iloc[i]['geometry'])
                    tmp.loc[i,'project'] = proj
                else:
                    proj = tmp['project'].iloc[i]
                dis = tmp.iloc[i+1]['geometry'].distance(tmp.iloc[i]['geometry'])
                if dis == 0:
                    proj1 = proj
                else:
                    proj2 = lineshp.project(tmp.iloc[i+1]['geometry'])
                    if abs(proj2-proj)>dis:
                        proj1 = np.sign(proj2-proj)*dis+proj
                    else:
                        proj1 = proj2
                tmp.loc[i+1,'project'] = proj1
            tmps.append(tmp)
        data = pd.concat(tmps)
    print('.',end = '')
    #公交站点也进行project
    stop = stop.to_crs(epsg = project_epsg)
    stop['project'] = stop['geometry'].apply(lambda r:lineshp.project(r))
    print('.',end = '')
    #标准化时间
    starttime = data[GPSDateTime].min()
    data['time_st'] = (data[GPSDateTime]-starttime).dt.total_seconds()
    BUS_project = data
    print('.')
    from shapely.geometry import LineString,Polygon
    import shapely

    #定义一个空的list存储识别结果
    ls = []

    print('Matching arrival and leaving info...',end = '')
    #对每一辆车遍历
    for car in BUS_project[VehicleId].drop_duplicates():
        print('.',end = '')
        #提取车辆轨迹
        tmp = BUS_project[BUS_project[VehicleId] == car]
        #如果车辆数据点少于1个，则无法构成轨迹
        if len(tmp)>1:
            #对每一个站点识别
            for stopname in stop[stopcol].drop_duplicates():
                #提取站点位置
                position = stop[stop[stopcol] == stopname]['project'].iloc[0]
                #通过缓冲区与线段交集识别到离站轨迹
                buffer_polygon = LineString([[0,position],
                                             [data['time_st'].max(),position]]).buffer(stopbuffer)
                bus_linestring = LineString(tmp[['time_st','project']].values)
                line_intersection = bus_linestring.intersection(buffer_polygon)
                #整理轨迹，提取到离站时间
                if line_intersection.is_empty:
                    #如果为空，说明车辆没有到站信息
                    continue
                else:
                    if type(line_intersection) == shapely.geometry.linestring.LineString:
                        arrive = [line_intersection]
                    else:
                        arrive = list(line_intersection)
                arrive = pd.DataFrame(arrive)
                arrive['arrivetime']= arrive[0].apply(lambda r:r.coords[0][0])
                arrive['leavetime']= arrive[0].apply(lambda r:r.coords[-1][0])

                #通过时间阈值筛选到离站信息
                a = arrive[['arrivetime']].copy()
                a.columns = ['time']
                a['flag'] = 1
                b = arrive[['leavetime']].copy()
                b.columns = ['time']
                b['flag'] = 0
                c = pd.concat([a,b]).sort_values(by = 'time')
                c['time1'] = c['time'].shift(-1)
                c['flag_1'] = ((c['time1']-c['time'])<mintime)&(c['flag']==0)
                c['flag_2'] = c['flag_1'].shift().fillna(False)
                c['flag_3'] = c['flag_1']|c['flag_2']
                c = c[-c['flag_3']]
                arrive_new = c[c['flag'] == 1][['time']].copy()
                arrive_new.columns = ['arrivetime']
                arrive_new['leavetime'] = list(c[c['flag'] == 0]['time'])
                arrive_new[stopcol] = stopname
                arrive_new[VehicleId] = car
                #合并数据
                ls.append(arrive_new)
    #合成一个大表
    arrive_info = pd.concat(ls)
    arrive_info['arrivetime'] = starttime+arrive_info['arrivetime'].apply(lambda r:pd.Timedelta(int(r),unit = 's'))
    arrive_info['leavetime'] = starttime+arrive_info['leavetime'].apply(lambda r:pd.Timedelta(int(r),unit = 's'))
    if projectoutput:
        return arrive_info,data
    else:
        return arrive_info


def busgps_onewaytime(arrive_info,start,end,col = ['VehicleId','stopname','arrivetime','leavetime']):
    '''
    Input the departure information table drive_info and the station information table stop to calculate the one-way travel time

    Parameters
    -------
    arrive_info : DataFrame
        The departure information table drive_info
    start : Str
        Starting station name
    end : Str
        Ending station name
    col : List
        Column name [vehicle ID, station name,arrivetime,leavetime]

    
    Returns
    -------
    onewaytime : DataFrame
        One-way travel time of the bus
    '''
    #上行
    #将起终点的信息提取后合并到一起
    #终点站的到达时间
    [VehicleId,stopname,arrivetime,leavetime] = col
    arrive_info[arrivetime] = pd.to_datetime(arrive_info[arrivetime])
    arrive_info[leavetime] = pd.to_datetime(arrive_info[leavetime])
    a = arrive_info[arrive_info[stopname] == end][[arrivetime,stopname,VehicleId]]
    #起点站的离开时间
    b = arrive_info[arrive_info[stopname] == start][[leavetime,stopname,VehicleId]]
    a.columns = ['time',stopname,VehicleId]
    b.columns = ['time',stopname,VehicleId]
    #合并信息
    c = pd.concat([a,b])
    #排序后提取每一单程的出行时间
    c = c.sort_values(by = [VehicleId,'time'])
    for i in c.columns:
        c[i+'1'] = c[i].shift(-1)
    #提取以申昆路枢纽站为起点，延安东路外滩为终点的趟次
    c = c[(c[VehicleId] == c[VehicleId+'1'])&
          (c[stopname]==start)&
          (c[stopname+'1']==end)]
    #计算该趟出行的持续时间
    c['duration'] = (c['time1'] - c['time']).dt.total_seconds()
    #标识该趟出行的时间中点在哪一个小时
    c['shour'] = c['time'].dt.hour
    c['direction'] = start+'-'+end
    #储存为c1变量
    c1 = c.copy()
    #下行
    a = arrive_info[arrive_info[stopname] == start][['arrivetime',stopname,VehicleId]]
    b = arrive_info[arrive_info[stopname] == end][['leavetime',stopname,VehicleId]]
    a.columns = ['time',stopname,VehicleId]
    b.columns = ['time',stopname,VehicleId]
    c = pd.concat([a,b])
    c = c.sort_values(by = [VehicleId,'time'])
    for i in c.columns:
        c[i+'1'] = c[i].shift(-1)
    c = c[(c[VehicleId] == c[VehicleId+'1'])&(c[stopname]==end)&(c[stopname+'1']==start)]
    c['duration'] = (c['time1'] - c['time']).dt.total_seconds()
    c['shour'] = c['time'].dt.hour
    c['direction'] = end+'-'+start
    c2 = c.copy()
    onewaytime = pd.concat([c1,c2])
    return onewaytime

