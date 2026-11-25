import sys, numpy as np, trimesh
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d.art3d import Poly3DCollection

def render(stl, out, views=((55,-60,"oblicua"),(90,-90,"planta"),(-60,-60,"inferior"))):
    tm=trimesh.load(stl); v=tm.vertices; f=tm.faces
    n=tm.face_normals; light=np.array([0.4,-0.5,0.8]); light/=np.linalg.norm(light)
    sh=np.clip(n@light,0,1)*0.65+0.35
    col=np.c_[sh*0.85,sh*0.55,sh*0.2,np.ones(len(f))]  # naranja
    fig=plt.figure(figsize=(5*len(views),5),dpi=110)
    c=tm.bounds.mean(0); r=(tm.bounds[1]-tm.bounds[0]).max()/2*0.62
    for k,(el,az,name) in enumerate(views):
        ax=fig.add_subplot(1,len(views),k+1,projection="3d")
        ax.add_collection3d(Poly3DCollection(v[f],facecolors=col,edgecolor="none",linewidths=0))
        ax.set_xlim(c[0]-r,c[0]+r); ax.set_ylim(c[1]-r,c[1]+r); ax.set_zlim(c[2]-r,c[2]+r)
        ax.set_box_aspect((1,1,1)); ax.view_init(el,az); ax.set_axis_off(); ax.set_title(name,fontsize=10)
    fig.tight_layout(); fig.savefig(out,facecolor="white"); plt.close(fig)

if __name__=="__main__":
    render(sys.argv[1],sys.argv[2])
